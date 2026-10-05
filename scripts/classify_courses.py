#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import re
import tempfile
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_PATH = ROOT / "data" / "expansion" / "expertise-taxonomy-v1.json"
CLASSIFIER_VERSION = "rules-v3"
GENERIC_TOPIC_TERMS = {"science", "design", "operations", "strategy", "management", "leadership",
                       "policy", "language", "visual", "molecular", "clinical", "intelligence", "college",
                       "honors", "thesis", "extension", "first-year", "independent study", "graduate research"}
GENERIC_PROGRAM_LABELS = {"general education", "general studies", "first year seminar program",
                          "first-year seminar program", "house seminars", "special concentrations",
                          "university courses", "additional courses offered", "extension"}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_taxonomy(taxonomy: dict[str, Any]) -> None:
    if not isinstance(taxonomy.get("version"), str) or not taxonomy["version"].strip():
        raise ValueError("Taxonomy requires an explicit version")
    ids_by_section = {}
    for section, term_field in (("academic_areas", "department_terms"), ("expertise_domains", "terms")):
        rows = taxonomy.get(section)
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"Taxonomy requires {section}")
        ids = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
                raise ValueError(f"Taxonomy has missing or duplicate {section} identifiers")
            if not isinstance(row.get("label"), str) or not row["label"].strip():
                raise ValueError(f"Taxonomy requires a label for {row['id']}")
            terms = row.get(term_field)
            if not isinstance(terms, list) or not terms or any(not isinstance(term, str) or not normalize(term) for term in terms):
                raise ValueError(f"Taxonomy requires nonempty phrases for {row['id']}")
            ids.add(row["id"])
        ids_by_section[section] = ids
    for domain in taxonomy["expertise_domains"]:
        parents = domain.get("academic_area_ids")
        if not isinstance(parents, list) or not parents or any(not isinstance(value, str) for value in parents):
            raise ValueError(f"Taxonomy requires parent areas for {domain['id']}")
        if len(parents) != len(set(parents)) or not set(parents).issubset(ids_by_section["academic_areas"]):
            raise ValueError(f"Taxonomy has duplicate or unknown parent areas for {domain['id']}")
    seen_contexts = set()
    for context in taxonomy.get("institution_subject_contexts", []):
        aliases = context.get("institution_aliases")
        if not isinstance(aliases, list) or not aliases or any(not isinstance(v, str) or not normalize(v) for v in aliases):
            raise ValueError("Subject context requires institution aliases")
        if not isinstance(context.get("source_url"), str) or not context["source_url"].startswith("https://"):
            raise ValueError("Subject context requires an official HTTPS source URL")
        prefixes = set()
        for subject in context.get("subjects", []):
            prefix = normalize(subject.get("prefix"))
            if not prefix or prefix in prefixes or not normalize(subject.get("department")):
                raise ValueError("Subject context requires distinct prefixes and department labels")
            parents = subject.get("academic_area_ids")
            if not isinstance(parents, list) or not parents or len(parents) != len(set(parents)) or not set(parents).issubset(ids_by_section["academic_areas"]):
                raise ValueError("Subject context requires distinct known academic areas")
            prefixes.add(prefix)
        for alias in aliases:
            if normalize(alias) in seen_contexts:
                raise ValueError("Institution subject context aliases must be unique")
            seen_contexts.add(normalize(alias))


def bind_merged_fingerprints(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["base_source_fingerprint"] = record.get("base_source_fingerprint", record.get("source_fingerprint", ""))
    result["base_classification_fingerprint"] = record.get("base_classification_fingerprint", record.get("classification_fingerprint", ""))
    result["source_fingerprint"] = fingerprint({
        "base": result["base_source_fingerprint"],
        "source_records": record.get("source_records", []),
        "source_urls": record.get("source_urls", []),
        "departments": record.get("departments", []),
        "description_variants": record.get("description_variants", []),
    })
    result["classification_fingerprint"] = fingerprint({
        "base": result["base_classification_fingerprint"],
        "source_fingerprint": result["source_fingerprint"],
        **{field: record.get(field) for field in ("academic_areas", "expertise_tags", "primary_academic_area",
                                                "primary_candidates", "primary_resolution", "classification_status",
                                                "review_reasons", "classification_variants")},
    })
    return result


def normalize(value: Any) -> str:
    return " ".join(str(value or "").replace("\u200b", "").casefold().split())


@lru_cache(maxsize=4096)
def term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")


def contains(text: str, term: str) -> bool:
    term = normalize(term)
    return bool(term and term_pattern(term).search(normalize(text)))


@lru_cache(maxsize=8)
def prepared_terms(taxonomy_json: str) -> dict[str, Any]:
    taxonomy = json.loads(taxonomy_json)
    validate_taxonomy(taxonomy)
    taxonomy["taxonomy_fingerprint"] = fingerprint(taxonomy)
    for section, field in (("academic_areas", "department_terms"), ("expertise_domains", "terms")):
        for item in taxonomy[section]:
            item["matchers"] = [(term, normalize(term), term_pattern(normalize(term))) for term in item[field]]
            if section == "academic_areas":
                terms = list(dict.fromkeys([term for term in item[field] if normalize(term) not in GENERIC_TOPIC_TERMS]
                                           + item.get("title_terms", [])))
                item["topic_matchers"] = [(term, normalize(term), term_pattern(normalize(term))) for term in terms]
    taxonomy["context_lookup"] = {
        normalize(alias): {normalize(subject["prefix"]): dict(subject, source_url=context["source_url"])
                           for subject in context["subjects"]}
        for context in taxonomy.get("institution_subject_contexts", []) for alias in context["institution_aliases"]}
    return taxonomy


def prepare_taxonomy(taxonomy: dict[str, Any]) -> dict[str, Any]:
    if taxonomy.get("taxonomy_fingerprint") and all("matchers" in item for item in taxonomy["academic_areas"]):
        return taxonomy
    return prepared_terms(json.dumps(taxonomy, sort_keys=True, ensure_ascii=False))


def matched_evidence(fields: list[tuple[str, str]], matchers: list[Any]) -> list[dict[str, str]]:
    return [{"field": field, "phrase": term} for field, text in fields
            for term, normalized_term, pattern in matchers if normalized_term in text and pattern.search(text)]


def unresolved_area() -> dict[str, Any]:
    return {"id": "other-academic-subject", "label": "Other academic subject",
            "matched_department_terms": [], "matched_title_terms": [],
            "matched_school_or_program_terms": [], "matched_description_terms": [],
            "matched_evidence": [], "basis": "unmapped", "evidence_strength": [0, 0]}


def department_labels(course: dict[str, Any]) -> list[str]:
    values = [course.get("catalog_department", ""), course.get("academic_subject", ""),
              course.get("subject_code", "")]
    values.extend(course.get("subject_areas", []) if isinstance(course.get("subject_areas"), list) else [])
    for department in course.get("departments", []):
        if isinstance(department, dict):
            values.extend((department.get("displayName", ""), department.get("name", ""),
                           department.get("id", ""), department.get("sisId", "")))
        elif isinstance(department, str):
            values.append(department)
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


def school_labels(course: dict[str, Any]) -> list[str]:
    values = [course.get(key, "") for key in ("school", "college")]
    return list(dict.fromkeys(value.strip() for value in values
                              if isinstance(value, str) and value.strip()))


def course_code(course: dict[str, Any]) -> Any:
    return course.get("code") or course.get("course_code")


def course_title(course: dict[str, Any]) -> Any:
    return course.get("title") or course.get("course_title")


def institution_subject_context(course: dict[str, Any], taxonomy: dict[str, Any]) -> dict[str, Any]:
    institution = normalize(course.get("institution") or course.get("university") or course.get("school"))
    lookup = taxonomy["context_lookup"].get(institution, {})
    raw_code = str(course_code(course) or "").strip()
    prefix_match = re.match(r"^(\d+[A-Za-z]*|[A-Za-z]+)(?=[.\s\d-]|$)", raw_code)
    prefix = normalize(course.get("subject_code") or (prefix_match.group(1) if prefix_match else ""))
    context = lookup.get(prefix, {})
    return dict(context, matched_field="subject_code" if course.get("subject_code") else
                ("code" if course.get("code") else "course_code")) if context else {}


def course_topic_evidence(fields: list[tuple[str, str]], area: dict[str, Any]) -> list[dict[str, str]]:
    evidence = matched_evidence(fields, area["topic_matchers"])
    if area["id"] == "health-medicine":
        nonhuman = any(re.search(r"\b(?:animal|plant|insect|veterinary)\b", text) for _, text in fields)
        if nonhuman:
            evidence = [item for item in evidence if normalize(item["phrase"]) not in
                        {"physiology", "pathology", "anatomy", "nutrition"}]
    return evidence


def topic_strength(area: dict[str, Any]) -> int:
    return max((len(item["phrase"].split()) for item in area.get("matched_evidence", [])
                if item["field"] in {"title", "course_title"}), default=0)


def primary_area_candidates(areas: list[dict[str, Any]]) -> tuple[list[str], str]:
    specific = [area for area in areas if area["id"] != "interdisciplinary-general"] or areas
    tier = max(area["evidence_strength"][0] for area in specific)
    strongest = [area for area in specific if area["evidence_strength"][0] == tier]
    topic_best = max(topic_strength(area) for area in strongest)
    if tier >= 4 and len(strongest) > 1 and topic_best:
        strongest = [area for area in strongest if topic_strength(area) == topic_best]
        basis = "specific-course-topic-within-department"
    else:
        strength = max(area["evidence_strength"] for area in strongest)
        strongest = [area for area in strongest if area["evidence_strength"] == strength]
        basis = strongest[0]["basis"]
    return sorted(area["id"] for area in strongest), basis


def classify_course(course: dict[str, Any], taxonomy: dict[str, Any]) -> dict[str, Any]:
    departments = department_labels(course)
    schools = school_labels(course)
    code = course_code(course)
    title = course_title(course)
    title_fields = [("code" if course.get("code") else "course_code", code),
                    ("title" if course.get("title") else "course_title", title),
                    ("subject_code", course.get("subject_code"))]
    normalized_title = [(field, normalize(value)) for field, value in title_fields if value]
    description = [("description", normalize(course.get("description")))] if course.get("description") else []
    prepared = prepare_taxonomy(taxonomy)
    context = institution_subject_context(course, prepared)
    specific_departments = [label for label in departments if normalize(label) not in GENERIC_PROGRAM_LABELS]
    broad_departments = [label for label in departments if normalize(label) in GENERIC_PROGRAM_LABELS]
    field_groups = [("official_department", 4, [("official_department_labels", normalize(label)) for label in specific_departments]),
                    ("official_school", 2, [("official_school_labels", normalize(label)) for label in schools]),
                    ("course_title_or_subject_code", 3, normalized_title),
                    ("course_description", 1, description)]
    bookstore_only = course.get("offering_status") == "bookstore-listed/registrar-unverified"
    matched_areas = []
    for area in ([] if bookstore_only else prepared["academic_areas"]):
        groups = [(basis, tier, course_topic_evidence(fields, area) if tier in {1, 3} else matched_evidence(fields, area["matchers"]))
                  for basis, tier, fields in field_groups]
        if area["id"] in context.get("academic_area_ids", []):
            groups.append(("institution-subject-context", 4, [{"field": context["matched_field"], "phrase": context["prefix"]}]))
        if broad_departments and area["id"] == "interdisciplinary-general":
            groups.append(("generic-program-context", 0, [{"field": "official_department_labels", "phrase": label}
                                                        for label in broad_departments]))
        evidence = [item for _, _, matches in groups for item in matches]
        if evidence:
            basis, tier, best = max((group for group in groups if group[2]),
                                    key=lambda group: (group[1], max(len(item["phrase"].split()) for item in group[2])))
            matched_areas.append({"id": area["id"], "label": area["label"],
                                  "matched_department_terms": sorted({item["phrase"] for item in groups[0][2]}),
                                  "matched_school_or_program_terms": sorted({item["phrase"] for item in groups[1][2]}),
                                  "matched_title_terms": sorted({item["phrase"] for item in groups[2][2]}),
                                  "matched_description_terms": sorted({item["phrase"] for item in groups[3][2]}),
                                  "matched_evidence": evidence, "basis": basis,
                                  "evidence_strength": [tier, max(len(item["phrase"].split()) for item in best)]})

    parse_status = str(course.get("parse_status") or course.get("catalog_description_status") or "").casefold()
    parse_needs_review = parse_status in {
        "needs-review", "source-page-unavailable", "unparsed", "incomplete", "title-only-reference",
    }
    unmatched = not matched_areas
    if unmatched:
        matched_areas = [unresolved_area()]
    candidates, primary_selection_basis = primary_area_candidates(matched_areas)
    strength = max(area["evidence_strength"] for area in matched_areas if area["id"] in candidates)
    primary = candidates[0] if len(candidates) == 1 else "other-academic-subject"
    reasons = []
    if unmatched:
        reasons.append("bookstore-identity-unverified" if bookstore_only else "unmapped-area")
    if parse_needs_review:
        reasons.append("source-parse-needs-review")
    if len(candidates) > 1:
        reasons.append("conflicting-primary-areas")
        matched_areas.append(unresolved_area())
    if strength[0] == 1:
        reasons.append("description-only-area")
    matched_areas.sort(key=lambda area: (area["id"] == "other-academic-subject",
                                        area["id"] == "interdisciplinary-general",
                                        -area["evidence_strength"][0], -area["evidence_strength"][1], area["id"]))

    expertise_tags = []
    for domain in ([] if bookstore_only else prepared["expertise_domains"]):
        evidence = matched_evidence(normalized_title + description, domain["matchers"])
        if evidence:
            expertise_tags.append({"id": domain["id"], "label": domain["label"],
                                   "matched_terms": sorted({item["phrase"] for item in evidence}),
                                   "matched_evidence": evidence,
                                   "academic_area_ids": domain["academic_area_ids"]})

    result = {
        "course_key": course.get("course_key", ""),
        "institution": course.get("institution") or course.get("university"),
        "code": code,
        "title": title,
        "course_record_id": course.get("course_record_id", ""),
        "source_urls": course.get("source_urls") or ([course["source_url"]] if course.get("source_url") else []),
        "academic_areas": matched_areas,
        "primary_academic_area": primary,
        "primary_candidates": candidates,
        "primary_selection_basis": primary_selection_basis,
        "primary_resolution": "unresolved" if unmatched or len(candidates) > 1 else "resolved",
        "expertise_tags": expertise_tags,
        "classification_method": "field-grounded subject and specialty phrase rules",
        "classification_version": CLASSIFIER_VERSION,
        "taxonomy_version": taxonomy["version"],
        "taxonomy_fingerprint": prepared["taxonomy_fingerprint"],
        "classification_status": "needs-review" if reasons else "candidate",
        "review_reasons": reasons,
        "source_fingerprint": fingerprint(course),
        "classification_evidence": {
            "official_department_labels": [] if bookstore_only else departments,
            "official_school_labels": [] if bookstore_only else schools,
            "institution_subject_context": {} if bookstore_only else context,
            "title_fields_used": [field for field, value in (("code" if course.get("code") else "course_code", code),
                                                               ("title" if course.get("title") else "course_title", title),
                                                               ("subject_code", course.get("subject_code"))) if value and not bookstore_only],
            "expertise_text_fields_used": [field for field, value in (
                ("code" if course.get("code") else "course_code", code),
                ("title" if course.get("title") else "course_title", title),
                ("description", course.get("description")),
                ("subject_code", course.get("subject_code"))) if value and not bookstore_only],
            "source_text_fields": {field: value for field, value in title_fields + [("description", course.get("description"))]
                                   if value},
            "source_parse_status": course.get("parse_status") or course.get("catalog_description_status"),
        },
    }
    result["classification_fingerprint"] = fingerprint(result)
    return result


def classify_inventory(source_dir: Path, taxonomy_path: Path = TAXONOMY_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    taxonomy = prepare_taxonomy(json.loads(taxonomy_path.read_text(encoding="utf-8")))
    output_manifest = {key: value for key, value in manifest.items() if key != "files"}
    output_manifest["classification_version"] = taxonomy["version"]
    output_manifest["classifier_version"] = CLASSIFIER_VERSION
    output_manifest["taxonomy_fingerprint"] = taxonomy["taxonomy_fingerprint"]
    output_manifest["classification_note"] = taxonomy["review_policy"]
    files = []
    for entry in manifest["files"]:
        if entry.get("collection", "courses") != "courses":
            continue
        if Path(entry["file"]).name != entry["file"]:
            raise ValueError(f"Inventory filename must not escape its source directory: {entry['file']}")
        compressed = (source_dir / entry["file"]).read_bytes()
        if hashlib.sha256(compressed).hexdigest() != entry["compressed_sha256"]:
            raise ValueError(f"SHA-256 mismatch: {entry['file']}")
        payload = json.loads(lzma.decompress(compressed))
        rows = payload["courses"]
        if not isinstance(rows, list) or len(rows) != entry["records"]:
            raise ValueError(f"Inventory count mismatch: {entry['file']}")
        keys = [row.get("course_key") for row in rows]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError(f"Inventory requires distinct course keys: {entry['file']}")
        counts: Counter[str] = Counter()
        tag_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        classification_rows = []
        for row in rows:
            classification = classify_course(row, taxonomy)
            counts[classification["primary_academic_area"]] += 1
            tag_counts.update(tag["id"] for tag in classification["expertise_tags"])
            status_counts[classification["classification_status"]] += 1
            classification_rows.append({
                key: row.get(key) for key in (
                    "course_key", "institution", "course_record_id", "code", "title",
                    "subject_code", "catalog_department", "career", "college", "source_urls"
                )
            } | classification)
        output = {"institution": payload["institution"], "classifications": classification_rows}
        raw = (json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        data = lzma.compress(raw, preset=6)
        files.append({"file": entry["file"], "institution": entry.get("institution", payload.get("institution", "")),
                      "records": len(rows), "primary_area_counts": dict(sorted(counts.items())),
                      "expertise_tag_counts": dict(sorted(tag_counts.items())),
                      "classification_status_counts": dict(sorted(status_counts.items())),
                      "review_needed": status_counts.get("needs-review", 0),
                      "unmapped_area_count": counts.get("other-academic-subject", 0),
                      "compressed_bytes": len(data), "compressed_sha256": hashlib.sha256(data).hexdigest(),
                      "data": data})
    output_manifest["files"] = [{key: value for key, value in entry.items() if key != "data"}
                                for entry in files]
    return output_manifest, files


def rebuild_indexes(root: Path, *, replace: bool = False) -> list[dict[str, Any]]:
    root = root.resolve()
    index = json.loads((root / "catalog-datasets-2026-27.json").read_text(encoding="utf-8"))
    pairs = index.get("dataset_pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("Dataset index requires source/classification pairs")
    planned = []
    destinations = set()
    source_names = {row["inventory"] for row in pairs}
    for pair in pairs:
        source_name, destination_name = pair["inventory"], pair["classification_index"]
        if any(not isinstance(name, str) or not name or Path(name).name != name for name in (source_name, destination_name)):
            raise ValueError("Dataset paths must be direct children of the expansion root")
        source, destination = (root / source_name).resolve(), (root / destination_name).resolve()
        if source.parent != root or destination.parent != root or destination_name in source_names:
            raise ValueError("Classification destination must not overlap any inventory or escape the expansion root")
        if destination in destinations:
            raise ValueError("Each dataset requires a distinct classification destination")
        destinations.add(destination)
        if destination.exists() and any(destination.iterdir()) and not replace:
            raise ValueError(f"Use --replace to rebuild existing derived index: {destination}")
        planned.append((source, destination))
    staged = []
    with tempfile.TemporaryDirectory(prefix=".classification-rebuild-", dir=root) as directory:
        staging = Path(directory)
        for source, destination in planned:
            manifest, files = classify_inventory(source, root / "expertise-taxonomy-v1.json")
            folder = staging / destination.name
            folder.mkdir()
            for entry in files:
                (folder / entry["file"]).write_bytes(entry["data"])
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            summary = {"source": source.name, "classification_index": destination.name,
                       "records": sum(entry["records"] for entry in manifest["files"]),
                       "review_needed": sum(entry["review_needed"] for entry in manifest["files"])}
            staged.append((folder, destination, manifest, summary))
            print(json.dumps({"staged": summary}, sort_keys=True), flush=True)
        for folder, destination, manifest, _ in staged:
            destination.mkdir(parents=True, exist_ok=True)
            for entry in manifest["files"]:
                os.replace(folder / entry["file"], destination / entry["file"])
            os.replace(folder / "manifest.json", destination / "manifest.json")
    return [summary for _, _, _, summary in staged]


def main() -> int:
    parser = argparse.ArgumentParser(description='Add reviewable subject-area and cross-cutting expertise candidates to a catalog.')
    parser.add_argument("--source", type=Path,
                        default=ROOT / "data" / "expansion" / "course-inventory-2026-27")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "data" / "expansion" / "classified-2026-27")
    parser.add_argument("--all", action="store_true", help="Rebuild all registered classification indexes from preserved inventories.")
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "expansion")
    parser.add_argument("--replace", action="store_true", help="Replace only derived indexes when rebuilding all registered pairs.")
    args = parser.parse_args()
    try:
        if args.all:
            reports = rebuild_indexes(args.root, replace=args.replace)
            print(json.dumps({"datasets": len(reports), "records": sum(item["records"] for item in reports)}, sort_keys=True))
            return 0
        if args.replace:
            raise ValueError("--replace requires --all")
        manifest, files = classify_inventory(args.source)
        out = args.out.resolve()
        if out.exists() and any(out.iterdir()):
            raise ValueError(f"Choose a new or empty output directory: {out}")
        out.mkdir(parents=True, exist_ok=True)
        for entry in files:
            (out / entry["file"]).write_bytes(entry["data"])
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
        print(f"Classified {sum(x['records'] for x in manifest['files'])} catalog courses to {out}")
    except (OSError, ValueError, KeyError, TypeError, lzma.LZMAError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
