#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import lzma
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

try:
    from .catalog import load_catalog
    from .course_map import load_course_map
    from .classify_courses import classify_course
    from .jev_classification_overlay import apply_record, load_overlay, load_discovery_supplements, apply_discovery_supplements
except ImportError:
    from catalog import load_catalog
    from course_map import load_course_map
    from classify_courses import classify_course
    from jev_classification_overlay import apply_record, load_overlay, load_discovery_supplements, apply_discovery_supplements

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_NAME = "course-materials-2026-27"
CLASSIFICATION_FIELDS = (
    "academic_areas", "primary_candidates", "primary_resolution", "classification_status", "classification_method",
    "classification_version", "taxonomy_version", "taxonomy_fingerprint", "source_fingerprint",
    "classification_fingerprint", "classification_evidence", "review_reasons", "classification_variants",
    "primary_selection_basis", "original_primary_academic_area", "rule_classification", "model_classification",
    "primary_expertise", "original_expertise_tags", "discovery_academic_areas", "facet_classification",
    "literal_facet_classification", "public_subject_evidence", "bibliographic_subject_evidence", "source_subject_context_evidence",
)
VALID_STATUS = {
    "found", "searched-no-public-material", "source-access-limited",
    "ambiguous", "not-researched", "has-saved-reference-records",
    "explicit-no-textbook-stated", "searched-no-book-evidence",
}
NONRESOURCE_EVIDENCE_TYPES = {
    "textbook requirement statement", "no textbook statement", "assigned reading statement",
    "reading availability statement", "required reading statement",
}


def canonical_isbn(value: str) -> str:
    digits = re.sub(r"[^0-9Xx]", "", value or "").upper()
    if len(digits) == 10:
        body = "978" + digits[:9]
        check = (10 - sum(int(char) * (1 if index % 2 == 0 else 3)
                          for index, char in enumerate(body)) % 10) % 10
        candidate = body + str(check)
        return candidate if digits[-1] == _isbn10_check(digits[:9]) else ""
    if len(digits) == 13 and digits.isdigit():
        check = (10 - sum(int(char) * (1 if index % 2 == 0 else 3)
                          for index, char in enumerate(digits[:12])) % 10) % 10
        return digits if check == int(digits[-1]) else ""
    return ""


def _isbn10_check(body: str) -> str:
    total = sum((10 - index) * int(char) for index, char in enumerate(body))
    check = (11 - total % 11) % 11
    return "X" if check == 10 else str(check)


def normalize_text(value: Any) -> str:
    plain = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    return " ".join(plain.casefold().split())


def normalize_status(value: Any) -> str:
    raw = str(value or "").strip()
    folded = raw.casefold().replace("_", "-").replace(" ", "-")
    if folded in VALID_STATUS:
        return folded
    if "ambiguous" in folded:
        return "ambiguous"
    if "access-limited" in folded or "login" in folded or "restricted" in folded:
        return "source-access-limited"
    if "no-public-material" in folded or "no-material" in folded:
        return "searched-no-public-material"
    if "found" in folded:
        return "found"
    if "reference" in folded and "not" not in folded:
        return "has-saved-reference-records"
    return "not-researched"


def material_sources(row: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []

    def add(value: Any, scope: str = "") -> None:
        if isinstance(value, str) and value:
            item = {"source_url": value, "scope": scope} if scope else {"source_url": value}
        elif isinstance(value, dict) and value:
            item = dict(value)
            if scope:
                item.setdefault("scope", scope)
        else:
            return
        if item not in sources:
            sources.append(item)

    for field, scope in (("searched_sources", "per-course source search"),
                         ("lookup_sources", "per-course source search"),
                         ("sources_checked", "source check recorded in the course evidence row"),
                         ("materials_sources_checked", "institutional materials policy/access source")):
        values = row.get(field) or []
        if not isinstance(values, list):
            values = [values]
        for value in values:
            add(value, scope)
    add(row.get("identified_material_lookup"))
    for field in ("source_url", "catalog_description_source_url", "course_source_url"):
        add(row.get(field), "course description or course record")
    return sources


def material_gaps(row: dict[str, Any]) -> list[str]:
    gaps = row.get("gaps") or []
    if not isinstance(gaps, list):
        gaps = [str(gaps)]
    for field in ("status_note", "course_specific_material_search_note"):
        value = row.get(field)
        if value and value not in gaps:
            gaps.append(str(value))
    if (normalize_status(row.get("status")) in {"ambiguous", "not-researched", "searched-no-book-evidence"}
            and row.get("status_evidence")):
        value = str(row["status_evidence"])
        if value not in gaps:
            gaps.append(value)
    identified = row.get("identified_material_lookup")
    if isinstance(identified, dict) and identified.get("status"):
        value = str(identified["status"])
        if value not in gaps:
            gaps.append(value)
    return [str(gap) for gap in gaps if gap]


def read_source_rows(root: Path) -> dict[str, list[dict[str, Any]]]:
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    excluded = {OUTPUT_NAME}
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and path.name not in excluded):
        manifest_path = folder / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest.get("files", []):
            filename = entry["file"]
            if entry.get("collection") != "materials" and "material" not in filename.casefold():
                continue
            path = folder / filename
            source_bytes = path.read_bytes()
            expected_hash = entry.get("compressed_sha256") or entry.get("sha256")
            if expected_hash and hashlib.sha256(source_bytes).hexdigest() != expected_hash:
                raise ValueError(f"SHA-256 mismatch: {path}")
            if path.suffix.casefold() == ".xz":
                source_bytes = lzma.decompress(source_bytes)
            payload = json.loads(source_bytes)
            rows = payload.get("materials")
            if not isinstance(rows, list):
                rows = payload.get("courses")
            if not isinstance(rows, list):
                continue
            for row in rows:
                course_key = row.get("course_key")
                if not course_key:
                    continue
                material_rows = row.get("materials", [])
                if not isinstance(material_rows, list):
                    material_rows = []
                else:
                    material_rows = list(material_rows)
                for statement_field in ("catalog_statements", "catalog_material_mentions_without_citation"):
                    statements = row.get(statement_field, [])
                    if not isinstance(statements, list):
                        statements = [statements] if statements else []
                    for statement in statements:
                        if isinstance(statement, dict):
                            item = dict(statement)
                        else:
                            item = {"evidence_excerpt": str(statement)}
                        item.setdefault("type", item.get("statement_type") or "catalog material statement")
                        item.setdefault("source_url", row.get("source_url") or
                                        row.get("catalog_description_source_url", ""))
                        item.setdefault("recurrence_eligible", False)
                        item.setdefault("evidence_scope", "Catalog material statement; not an individually named resource.")
                        material_rows.append(item)
                by_key[course_key].append({
                    "ledger_id": f"{folder.name}/{filename}",
                    "ledger_institution": row.get("institution") or payload.get("institution") or manifest.get("institution", ""),
                    "status": normalize_status(row.get("status")),
                    "additional_statuses": sorted({
                        status for field in ("course_specific_material_search_status",
                                             "actual_materials_search_status")
                        if (status := normalize_status(row.get(field))) != "not-researched"
                    }),
                    "source_status": row.get("status", "not stated"),
                    "source_record": {key: value for key, value in row.items() if key != "materials"},
                    "materials": material_rows,
                    "sources": material_sources(row),
                    "gaps": material_gaps(row),
                    "evidence_excerpt": row.get("evidence_excerpt", ""),
                    "evidence_location": row.get("evidence_location", ""),
                    "checked_on": row.get("checked_on") or manifest.get("checked_on", ""),
                })
    return by_key


def material_entry(course: dict[str, Any], supplemental: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    supplemental = supplemental or []
    explicit_statuses = sorted({status for item in supplemental
                                for status in [item["status"], *item.get("additional_statuses", [])]})
    materials = []
    seen = set()
    sources = []
    gaps = []
    source_rows = []
    for ledger in supplemental:
        source_rows.append({key: value for key, value in ledger.items() if key != "materials"})
        for source in ledger.get("sources", []):
            if source and source not in sources:
                sources.append(source)
        for gap in ledger.get("gaps", []):
            if gap and gap not in gaps:
                gaps.append(gap)
        for material in ledger.get("materials", []):
            if not isinstance(material, dict):
                material = {"source_title_or_citation": str(material)}
            material = dict(material)
            material.setdefault("ledger_id", ledger["ledger_id"])
            key = json.dumps(material, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                materials.append(material)

    text_statuses = [
        "explicit-no-textbook-stated"
        if is_no_textbook_statement(item)
        else status_for_material_text(material_title(item))
        for item in materials
    ]
    text_statuses = [value for value in text_statuses if value]
    resource_items = [item for item in materials if is_resource_record(item)]
    evidence_statements = [item for item in materials if (
        material_kind(item) in NONRESOURCE_EVIDENCE_TYPES or is_no_textbook_statement(item) or
        (item.get("recurrence_eligible") is False and bool(item.get("evidence_excerpt"))) or
        bool(item.get("source_field")) or is_unparsed_material_block(material_title(item)))]
    if "found" in explicit_statuses and resource_items:
        status = "found"
    elif ("explicit-no-textbook-stated" in text_statuses or
          "explicit-no-textbook-stated" in explicit_statuses):
        status = "explicit-no-textbook-stated"
    elif "source-access-limited" in explicit_statuses or "source-access-limited" in text_statuses:
        status = "source-access-limited"
    elif "found" in explicit_statuses and evidence_statements:
        status = "found"
    elif "searched-no-public-material" in explicit_statuses:
        status = "searched-no-public-material"
    elif "ambiguous" in explicit_statuses or "ambiguous" in text_statuses:
        status = "ambiguous"
    elif "searched-no-book-evidence" in explicit_statuses:
        status = "searched-no-book-evidence"
    elif "has-saved-reference-records" in explicit_statuses:
        status = "has-saved-reference-records"
    elif "found" in explicit_statuses:
        status = "searched-no-public-material"
    elif explicit_statuses:
        status = explicit_statuses[0]
    else:
        status = normalize_status(course.get("materials_research_status"))

    if not sources:
        if course.get("source_urls"):
            sources = [{"source_url": url} for url in course["source_urls"]]
        elif course.get("source_url"):
            sources = [{"source_url": course["source_url"]}]
    if not gaps:
        note = course.get("materials_research_note")
        if note:
            gaps.append(note)
        elif status == "not-researched":
            gaps.append("Per-course books and other learning materials have not been researched in this source pass.")

    return {
        "course_key": course["course_key"],
        "institution": str(course.get("institution") or ""),
        "code": str(course.get("code") or ""),
        "title": str(course.get("title") or ""),
        "school": str(course.get("school") or ""),
        "primary_academic_area": course.get("primary_academic_area", ""),
        "expertise_tags": course.get("expertise_tags", []),
        **{field: course[field] for field in CLASSIFICATION_FIELDS if field in course},
        "status": status,
        "evidence_statuses": explicit_statuses,
        "materials": materials,
        "sources": sources,
        "gaps": gaps,
        "source_evidence_rows": source_rows,
        "source_records": course.get("source_records", []),
        "knowledge_processed": False,
    }


def baseline_material_entries(root: Path = ROOT, *, taxonomy: dict[str, Any] | None = None,
                              expansion_root: Path | None = None) -> list[dict[str, Any]]:
    data, _, manifest = load_catalog()
    if taxonomy is None:
        taxonomy = json.loads((root / "data/expansion/expertise-taxonomy-v1.json").read_text(encoding="utf-8"))
    model_overlay = load_overlay(expansion_root or root / "data/expansion", taxonomy)
    evidence_root = expansion_root or root / "data/expansion"
    supplements = load_discovery_supplements(evidence_root, taxonomy) if model_overlay is not None else {}
    courses = {row["course_id"]: row for row in data["courses"]}
    coverage = {row["course_id"]: row for row in data["book_coverage"]}
    books_by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for book in data["books"]:
        for course_id in book.get("course_ids", []):
            books_by_course[course_id].append(book)
    result = []
    for course_id, course in courses.items():
        classification_source = {**course, "course_key": f"topclass-v0.4:{course_id}",
                                 "institution": course.get("school", ""), "code": course.get("course_code", ""),
                                 "source_urls": [course["source_url"]] if course.get("source_url") else []}
        classification = classify_course(classification_source, taxonomy)
        if model_overlay is not None:
            classification_source["classification_evidence"] = classification["classification_evidence"]
            classification_source["source_records"] = [{"inventory": "exports/v0.4", "course_version": course.get("version", "")}]
            classification = apply_record(classification_source | classification,
                                          model_overlay.get(classification_source["course_key"]), taxonomy)
            urls = [book.get("source_url") for book in books_by_course.get(course_id, []) if book.get("source_url")]
            classification = apply_discovery_supplements(classification, model_overlay.get(classification_source["course_key"]),
                taxonomy, supplements, source_urls=urls)
        books = books_by_course.get(course_id, [])
        raw_status = coverage[course_id].get("book_research_status", "")
        policy = coverage[course_id].get("explicit_textbook_policy", "")
        if books:
            status = "has-saved-reference-records"
        elif "explicitly no textbook" in str(raw_status).casefold():
            status = "explicit-no-textbook-stated"
        elif "inspected" in str(raw_status).casefold():
            status = "searched-no-book-evidence"
        elif "restricted" in str(raw_status).casefold() or "unresolved" in str(raw_status).casefold():
            status = "source-access-limited"
        else:
            status = normalize_status(raw_status)
        material_rows = [{
            "material_id": book["book_id"],
            "resource_type": book.get("resource_type", "Book"),
            "exact_source_title_or_citation": book.get("title", ""),
            "authors": book.get("authors", ""),
            "edition": book.get("assigned_edition", ""),
            "isbn": book.get("isbn", ""),
            "isbn_validation": book.get("isbn_validation", ""),
            "assignment_role": book.get("assignment_category") or book.get("assignment_role", "not_stated"),
            "assigned_portions": book.get("assigned_portions", ""),
            "source_url": book.get("source_url", ""),
            "evidence_excerpt": book.get("evidence_summary", ""),
            "work_id": book.get("work_id", ""),
            "bibliographic_status": book.get("bibliographic_status", ""),
            "acquired": book.get("acquired", False),
            "scope": "Saved v0.4 course-to-reference record; assignment applies only to its cited course version",
        } for book in books]
        result.append({
            "course_key": f"topclass-v0.4:{course_id}",
            "baseline_course_id": course_id,
            "institution": course.get("school", ""),
            "code": course.get("course_code", ""),
            "title": course.get("title", ""),
            "school": course.get("school", ""),
            "primary_academic_area": classification["primary_academic_area"],
            "expertise_tags": classification["expertise_tags"],
            **{field: classification[field] for field in CLASSIFICATION_FIELDS if field in classification},
            "status": status,
            "evidence_statuses": [raw_status, policy],
            "materials": material_rows,
            "sources": ([{"source_url": row.get("source_url", ""), "source_id": row.get("source_id", "")}
                         for row in books if row.get("source_url")]),
            "gaps": [note for note in (coverage[course_id].get("notes", ""), policy)
                     if note and note != "Not established"],
            "source_evidence_rows": [],
            "source_records": [{"inventory": "exports/v0.4", "catalog_version": manifest["catalog_version"],
                                "course_version": course.get("version", "")}],
            "knowledge_processed": False,
        })
    return result


def isbn_from_material(material: dict[str, Any]) -> str:
    value = str(material.get("isbn") or "")
    if not value and material.get("association_status") == "library-course-reserve-candidate":
        return ""
    recurrence_key = str(material.get("recurrence_key") or "")
    if not value and recurrence_key.casefold().startswith("isbn:"):
        value = recurrence_key.split(":", 1)[1]
    if not value:
        citation = " ".join(str(material.get(key) or "") for key in
                             ("exact_source_title_or_citation", "source_title_or_citation", "citation", "exact_source_title"))
        for match in re.finditer(r"\bISBN(?:-1[03])?\s*:?\s*([0-9Xx][0-9Xx\- ]{8,20}[0-9Xx])\b", citation, re.I):
            canonical = canonical_isbn(match.group(1))
            if canonical:
                return canonical
        return ""
    validation = str(material.get("isbn_validation", "")).casefold()
    canonical = canonical_isbn(value)
    if not canonical:
        return ""
    if (validation.startswith("valid") or "checksum valid" in validation or
            recurrence_key.casefold().startswith("isbn:")):
        return canonical
    return ""


def is_nonresource_statement(title: str) -> bool:
    plain = html.unescape(re.sub(r"<[^>]*>", " ", title or ""))
    normalized = normalize_text(plain).strip(" .;:,-")
    patterns = (
        r"^(?:there are )?no (?:(?:standard|required) )?(?:books?|text ?books?|materials?|texts?)(?: are)?(?: required| needed| to purchase| used)?(?: for this course)?(?:[;,].*)?$",
        r"^no textbook is required(?: for this course)?(?:[;,].*)?$",
        r"^no required textbooks?$",
        r"^no textbooks? are required(?: for this course)?$",
        r"^(?:text ?books?|books?|course materials?) (?:are )?not required(?: for this course)?$",
        r"^(?:text ?books?|books?|course materials?) (?:are )?not used(?: for this course)?$",
        r"^(?:this course )?(?:does not require|will not use|uses no) (?:a )?(?:standard )?(?:text ?book|book|course materials?)(?:s)?(?:[;,].*)?$",
        r"^(?:no )?(?:standard )?text ?books? (?:will be )?used(?: for this course)?$",
        r"^none(?: required)?$",
        r"^n/?a$",
        r"^tba(?: by instructor)?$|^tbd(?: by instructor)?$",
        r"^(?:to be announced|to be determined|not yet selected|not yet determined)(?: by instructor)?$",
        r"^varies(?: by section)?$",
        r"^check with instructor$",
        r"^(?:all readings will be posted|all text info is in|materials will be posted)\b",
        r"^on canvas$|^(?:course )?readings? (?:are )?on canvas$|^all readings (?:are )?on canvas$",
        r"^no required textbook,? readings? on canvas$",
        r"^readings for this course are selected by the student$",
        r"^(?:text ?book|book|course materials?|reading list) details? (?:will be|to be) provided(?: .*)?$",
        r"^(?:text ?book|book|course materials?|reading list) (?:will be|to be) announced(?: .*)?$",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def status_for_material_text(title: str) -> str:
    plain = html.unescape(re.sub(r"<[^>]*>", " ", title or ""))
    normalized = normalize_text(plain).strip(" .;:,-")
    if is_nonresource_statement(title) and re.search(
            r"\bno (?:standard |required )?(?:books?|text ?books?|materials?|texts?)\b|"
            r"\bno textbook is required\b|\bnot required\b|\bnot used\b|\buses no\b|\bdoes not require\b",
            normalized):
        return "explicit-no-textbook-stated"
    if re.search(r"\b(?:canvas|courseworks|lms)\b", normalized):
        return "source-access-limited"
    if is_nonresource_statement(title):
        return "ambiguous"
    if re.search(r"^(?:tba|tbd|n/?a|none|varies|check with instructor|to be announced|to be determined)\b", normalized):
        return "ambiguous"
    return ""


def is_unparsed_material_block(title: str) -> bool:
    plain = html.unescape(re.sub(r"<[^>]*>", " ", title or ""))
    if len(plain.strip()) > 280:
        return True
    if re.search(r"\n\s*\n", plain):
        return True
    numbered_items = re.findall(r"(?:^|[\r\n])\s*\d+[.)]\s+", plain)
    if len(numbered_items) > 1:
        return True
    isbn_matches = re.findall(
        r"\bISBN(?:-1[03])?\s*:?\s*([0-9Xx][0-9Xx\- ]{8,20}[0-9Xx])\b", plain, re.I)
    if len(re.findall(r"\bISBN(?:-1[03])?\b", plain, re.I)) > 1:
        return True
    if len({canonical_isbn(value) for value in isbn_matches if canonical_isbn(value)}) > 1:
        return True
    return False


def course_identity(institution: str, code: str, course_key: str) -> str:
    school = normalize_text(institution)
    aliases = {
        "berkeley": "uc berkeley", "uc berkeley": "uc berkeley",
        "brown": "brown university", "brown university": "brown university",
        "carnegie mellon": "carnegie mellon university",
        "carnegie mellon university": "carnegie mellon university", "cmu": "carnegie mellon university",
        "columbia": "columbia university", "columbia university": "columbia university",
        "cornell": "cornell university", "cornell university": "cornell university",
        "dartmouth": "dartmouth college", "dartmouth college": "dartmouth college",
        "harvard": "harvard university", "harvard university": "harvard university",
        "mit": "massachusetts institute of technology",
        "massachusetts institute of technology": "massachusetts institute of technology",
        "penn": "university of pennsylvania", "university of pennsylvania": "university of pennsylvania",
        "princeton": "princeton university", "princeton university": "princeton university",
        "stanford": "stanford university", "stanford university": "stanford university",
        "yale": "yale university", "yale university": "yale university",
    }
    school = aliases.get(school, school)
    primary_code = str(code or "").split("/", 1)[0].strip()
    if not primary_code or normalize_text(primary_code) in {"n/a", "unknown", "no code listed"}:
        return f"course-key:{course_key}"
    primary_code = re.sub(r"^(?:MIT|CMU)\s+", "", primary_code, flags=re.I)
    return school + "|" + re.sub(r"\s+", "", primary_code.casefold())


def material_title(material: dict[str, Any]) -> str:
    return str(material.get("exact_source_title") or material.get("exact_source_title_or_citation") or
               material.get("source_title_or_citation") or material.get("citation") or
               material.get("title") or material.get("resource_title") or "")


def material_kind(material: dict[str, Any]) -> str:
    value = material.get("resource_type") or material.get("type") or material.get("material_type") or ""
    return normalize_text(value).replace("_", " ").replace("-", " ")


def material_assignment_role(material: dict[str, Any]) -> str:
    return str(material.get("assignment_role") or material.get("assigned_vs_suggested_wording") or
               material.get("assigned_or_suggested") or "not_stated")


def is_no_textbook_statement(material: dict[str, Any]) -> bool:
    kind = material_kind(material)
    return kind == "textbook requirement statement" or bool(
        re.search(r"\bno (?:required )?textbooks?\b", kind))


def is_resource_record(material: dict[str, Any]) -> bool:
    if material.get("recurrence_eligible") is False:
        return False
    kind = material_kind(material)
    if kind in NONRESOURCE_EVIDENCE_TYPES:
        return False
    title = material_title(material)
    if not title or is_nonresource_statement(title):
        return False
    bibliography = material.get("library_bibliography")
    if material.get("association_status") == "library-course-reserve-candidate" and kind == "book" and isinstance(bibliography, dict):
        titles = bibliography.get("title")
        if isinstance(titles, list) and any(isinstance(value, str) and value.strip() == title.strip() for value in titles):
            return True
    if status_for_material_text(title) or is_unparsed_material_block(title):
        return False
    if re.match(r"^catalog description explicitly (?:states|says)\b", title, re.I):
        return False
    return True


def resource_identity(material: dict[str, Any]) -> dict[str, str] | None:
    title = material_title(material)
    if not title or not is_resource_record(material):
        return None
    author = (material.get("authors") or material.get("author") or
              material.get("author_or_editor") or "")
    edition = (material.get("edition") or material.get("assigned_edition") or
               material.get("assigned_edition_or_version") or "")
    isbn = isbn_from_material(material)
    if isbn:
        key, basis, certainty = (f"isbn:{isbn}", "checksum-valid ISBN",
                                 "same ISBN candidate; compare title, author, and edition before acquisition merge")
    elif title and author:
        key = "citation:" + "|".join((normalize_text(title), normalize_text(author), normalize_text(edition)))
        basis, certainty = "normalized title, author, and edition", "bibliographic match candidate"
    elif title and edition:
        key = "title-edition:" + "|".join((normalize_text(title), normalize_text(edition)))
        basis, certainty = "normalized exact title and edition", "bibliographic match candidate; authors not established"
    else:
        key = "title-candidate:" + normalize_text(title)
        basis, certainty = "normalized exact title/citation", "bibliographic match candidate; authors/edition not established"
    return {"key": key, "identity_basis": basis, "certainty": certainty,
            "title": title, "authors": str(author), "edition": str(edition), "isbn": isbn}


def is_book_material(material: dict[str, Any]) -> bool:
    if not is_resource_record(material):
        return False
    for field in ("is_book_format", "is_book", "isBook"):
        if field not in material:
            continue
        value = material[field]
        folded = str(value).strip().casefold()
        if isinstance(value, bool) or folded in {"t", "true", "yes", "1", "f", "false", "no", "0"}:
            return value is True or folded in {"t", "true", "yes", "1"}
    kind = material_kind(material)
    if "chapter" in kind or "statement" in kind or "block" in kind:
        return False
    return bool(re.search(r"\b(?:book|textbook|e ?book|monograph|workbook)\b", kind))


def is_learning_book(material: dict[str, Any]) -> bool:
    if material_kind(material) in {"device", "equipment", "calculator", "classroom response device", "supplies"}:
        return False
    title = material_title(material).split(" / ", 1)[0].strip()
    if re.fullmatch(r"(?:ELEGOO\s+)?UNO R3 SUPER STARTER KIT", title, re.I):
        return False
    if re.fullmatch(r"i[ -]?clicker(?:\s*(?:2|plus|\+))?(?:\s+(?:student\s+)?(?:remote|remote control|response device))?", title, re.I):
        return False
    return is_book_material(material)


def book_metrics(entries: list[dict[str, Any]]) -> dict[str, Any]:
    source_records = 0
    links: set[tuple[str, str]] = set()
    courses: set[str] = set()
    identities: dict[str, dict[str, Any]] = {}
    source_course_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    for entry in entries:
        course_id = course_identity(entry.get("institution", ""), entry.get("code", ""),
                                    entry.get("course_key", ""))
        for material in entry.get("materials", []):
            if not isinstance(material, dict) or not is_book_material(material):
                continue
            identity = resource_identity(material)
            if not identity:
                continue
            source_records += 1
            courses.add(course_id)
            links.add((course_id, identity["key"]))
            source_course_keys[(course_id, identity["key"])].add(str(entry.get("course_key", "")))
            grouped = identities.setdefault(identity["key"], {
                "identity_basis": identity["identity_basis"], "course_identities": set(),
            })
            grouped["course_identities"].add(course_id)
    repeated_cross_course = 0
    for identity in identities.values():
        if len(identity["course_identities"]) > 1:
            repeated_cross_course += 1
    repeated_same_course_sources = sum(len(source_keys) > 1 for source_keys in source_course_keys.values())
    return {
        "named_book_material_record_count": source_records,
        "deduplicated_course_book_link_count": len(links),
        "distinct_book_identity_candidate_count": len(identities),
        "courses_with_named_book_evidence": len(courses),
        "repeated_book_candidate_groups_across_distinct_courses": repeated_cross_course,
        "repeated_book_candidate_groups_within_same_course_identity": repeated_same_course_sources,
        "book_candidate_identity_basis_counts": dict(sorted(Counter(
            identity["identity_basis"] for identity in identities.values()
        ).items())),
    }


def recurrence(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        for material in entry.get("materials", []):
            if not isinstance(material, dict):
                continue
            identity = resource_identity(material)
            if not identity:
                continue
            title = identity["title"]
            author = identity["authors"]
            edition = identity["edition"]
            isbn = identity["isbn"]
            source_id = str(material.get("ledger_id") or material.get("material_id") or "")
            groups[identity["key"]].append({
                "course_key": entry["course_key"], "institution": entry.get("institution", ""),
                "course_code": entry.get("code", ""), "course_title": entry.get("title", ""),
                "resource_type": material.get("resource_type") or material.get("type") or material.get("material_type", ""),
                "resource_title": title, "authors": author, "edition": edition,
                "isbn": isbn, "assignment_role": material_assignment_role(material),
                "source_url": material.get("source_url", ""), "evidence_excerpt": material.get("evidence_excerpt", ""),
                "source_identity": source_id, "identity_basis": identity["identity_basis"],
                "certainty": identity["certainty"],
            })
    repeated = []
    for key, rows in groups.items():
        unique_courses = {row["course_key"] for row in rows}
        if len(unique_courses) < 2:
            continue
        course_identities = {course_identity(row["institution"], row["course_code"], row["course_key"])
                             for row in rows}
        repeated.append({
            "resource_key": key,
            "identity_basis": rows[0]["identity_basis"],
            "certainty": rows[0]["certainty"],
            "resource_title": rows[0]["resource_title"],
            "authors": rows[0]["authors"],
            "edition": rows[0]["edition"],
            "isbn": rows[0]["isbn"],
            "distinct_course_count": len(unique_courses),
            "distinct_course_identity_count": len(course_identities),
            "recurrence_scope": ("cross-course" if len(course_identities) > 1 else
                                 "same course identity across saved catalog records"),
            "source_record_count": len(rows),
            "courses": sorted({(row["institution"], row["course_code"], row["course_title"], row["course_key"])
                               for row in rows}),
            "assignment_roles": dict(sorted(Counter(row["assignment_role"] for row in rows).items())),
            "source_urls": sorted({row["source_url"] for row in rows if row["source_url"]}),
            "evidence": rows,
        })
    repeated.sort(key=lambda row: (-row["distinct_course_count"], row["resource_title"].casefold()))
    return repeated


def build_material_index(root: Path = ROOT / "data" / "expansion") -> tuple[dict[str, Any], dict[str, Any]]:
    courses = load_course_map(root, include_materials=False)
    source_ledgers = read_source_rows(root)
    entries = [material_entry(course, source_ledgers.get(course["course_key"], [])) for course in courses]
    taxonomy = json.loads((root / "expertise-taxonomy-v1.json").read_text(encoding="utf-8"))
    entries.extend(baseline_material_entries(taxonomy=taxonomy, expansion_root=root))
    entries.sort(key=lambda row: (row["institution"].casefold(), row["code"].casefold(), row["course_key"]))
    overlaps = recurrence(entries)
    book_summary = book_metrics(entries)
    scoped_book_summaries = {
        "2026-27_expansion": book_metrics([entry for entry in entries if not entry.get("baseline_course_id")]),
        "v0.4_baseline": book_metrics([entry for entry in entries if entry.get("baseline_course_id")]),
    }
    status_counts = Counter(row["status"] for row in entries)
    material_records = sum(len(row["materials"]) for row in entries)
    by_institution: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in entries:
        by_institution[row["institution"]].append(row)
    institution_summary = {}
    for institution, rows in sorted(by_institution.items()):
        institution_summary[institution] = {
            "course_identity_entries": len(rows),
            "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
            "entries_with_material_records": sum(bool(row["materials"]) for row in rows),
            "entries_with_named_resource_evidence": sum(
                any(is_resource_record(material) for material in row["materials"])
                for row in rows
            ),
        }
    payload = {
        "catalog_year": "2026-27",
        "checked_on": date.today().isoformat(),
        "entries": entries,
        "recurrent_resources": overlaps,
        "knowledge_corpus_processed": False,
        "knowledge_recurrence": [],
        "knowledge_note": "No supplied book corpus has been processed. Repeated citations, titles, class descriptions, and material labels are not evidence that underlying knowledge was extracted or repeatedly used.",
    }
    manifest = {
        "catalog_year": "2026-27",
        "checked_on": payload["checked_on"],
        "course_identity_entries": len(entries),
        "status_counts": dict(sorted(status_counts.items())),
        "institutions": institution_summary,
        "entries_with_material_records": sum(bool(row["materials"]) for row in entries),
        "entries_with_specific_or_descriptive_resource_evidence": sum(
            any(is_resource_record(material) for material in row["materials"])
            for row in entries
        ),
        "entries_with_unidentified_material_evidence": sum(
            any(not is_resource_record(material) and bool(
                material_title(material) or material_kind(material) or
                material.get("evidence_excerpt") or material.get("assigned_or_suggested"))
                for material in row["materials"])
            for row in entries
        ),
        "material_record_count": material_records,
        **book_summary,
        "book_metrics_by_scope": scoped_book_summaries,
        "recurrent_resource_candidate_groups": len(overlaps),
        "repeated_resource_groups_across_distinct_courses": sum(
            row["distinct_course_identity_count"] > 1 for row in overlaps),
        "same_course_identity_resource_groups": sum(
            row["distinct_course_identity_count"] == 1 for row in overlaps),
        "knowledge_corpus_processed": False,
        "knowledge_recurrence_count": 0,
        "scope_note": "Course materials are recorded only where an official source exposed an explicit citation or a course-keyed search status. Unresearched and access-limited rows remain visible; they do not mean a course has no materials.",
        "status_guide": {
            "found": "An official source exposed a named resource or an explicit materials statement; this does not establish a complete term reading list.",
            "searched-no-public-material": "The recorded public course-material source was checked but yielded no named resource; this is not a no-material finding.",
            "searched-no-book-evidence": "The stated catalog-description or reference scope was checked and no book citation was identified; syllabi or other sources may still contain materials.",
            "source-access-limited": "A relevant per-course source was inaccessible or required authentication.",
            "ambiguous": "The source evidence could not be resolved to a course or a specific resource.",
            "not-researched": "No per-course materials source has been checked in this expansion pass.",
            "has-saved-reference-records": "The v0.4 baseline contains one or more saved course-to-reference records; this does not establish current adoption.",
            "explicit-no-textbook-stated": "An official source explicitly says no textbook is required; other readings or materials may still be used.",
        },
    }
    return payload, manifest


def write_snapshot(out_dir: Path, payload: dict[str, Any], manifest: dict[str, Any]) -> None:
    out_dir = out_dir.resolve()
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"Choose a new or empty output directory: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    compressed = lzma.compress(raw, preset=6)
    entry = {"file": "course-materials.json.xz", "collection": "entries",
             "records": len(payload["entries"]), "compressed_bytes": len(compressed),
             "compressed_sha256": hashlib.sha256(compressed).hexdigest()}
    manifest["files"] = [entry]
    (out_dir / entry["file"]).write_bytes(compressed)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
    print(f"Saved materials evidence for {len(payload['entries'])} course identities to {out_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description='Combine per-course material evidence and report recurring resource candidates.')
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "expansion")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "expansion" / OUTPUT_NAME)
    args = parser.parse_args()
    try:
        payload, manifest = build_material_index(args.root)
        write_snapshot(args.out, payload, manifest)
    except (OSError, ValueError, KeyError, TypeError, lzma.LZMAError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
