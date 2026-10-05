#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

try:
    from .bibliographic_findings import finding_aids
    from .catalog_identity import resolve_institution
    from .course_map import csv_value, read_payload
    from .materials_ledger import canonical_isbn, isbn_from_material, is_book_material, is_learning_book, material_assignment_role, material_title
    from .reading_enrichment import FOLDER, PUBLISHERS, ROOT, checked_url, child, plain, text
except ImportError:
    from bibliographic_findings import finding_aids
    from catalog_identity import resolve_institution
    from course_map import csv_value, read_payload
    from materials_ledger import canonical_isbn, isbn_from_material, is_book_material, is_learning_book, material_assignment_role, material_title
    from reading_enrichment import FOLDER, PUBLISHERS, ROOT, checked_url, child, plain, text


FORMAT = "topclass-isbn-evidence-v1"
PUBLISHER_DOMAINS = PUBLISHERS | {"ams.org", "oreilly.com", "hackettpublishing.com", "press.umich.edu",
                                  "stata.com", "stata-press.com", "sagepub.com", "penguinrandomhouse.com", "cengage.com.cn"}
RESOLUTION_FILES = ("isbn-evidence-a.json", "isbn-evidence-b.json")
FIELDS = ("university", "course_key", "course_code", "course_title", "book_title", "authors",
          "assigned_edition", "assigned_isbn", "assigned_isbn_raw", "matched_isbn", "candidate_isbn",
          "isbn_status", "isbn_origin", "library_edition", "format_name", "assignment_role", "course_source_url", "bibliographic_source_url",
          "source_year", "source_verification_status", "evidence_id", "material_index", "resolution_material_index", "course_citation",
          "publisher_title", "publisher_authors", "publisher_edition", "match_rationale", "title_match_rationale", "author_match_rationale",
          "bibliography_verified", "current_adoption_verified", "knowledge_processed", "finding_aids")
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
            "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}


def load_readings(root=ROOT, *, all_saved=False):
    folder = child(root, "course-materials-2026-27" if all_saved else FOLDER)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    collection = "entries" if all_saved else "materials"
    entries = [entry for entry in manifest["files"] if entry.get("collection") == collection]
    if len(entries) != 1:
        raise ValueError("Expected one manifest-registered materials snapshot")
    entry = entries[0]
    child(folder, entry["file"])
    return read_payload(folder, entry, collection)[collection]


def edition_identity(value):
    normalized = plain(value)
    normalized = re.sub(r"\b(?:edition|ed)\b", "", normalized).strip()
    if normalized in ORDINALS:
        return str(ORDINALS[normalized])
    number = re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", normalized)
    return number[1] if number else normalized


def book_isbn(value):
    try:
        normalized = canonical_isbn(value)
    except ValueError:
        return ""
    return normalized if normalized.startswith(("978", "979")) else ""


def source_isbn(material):
    raw = str(material.get("isbn") or "")
    if raw:
        return raw, book_isbn(raw)
    citations = [str(material.get(field) or "") for field in (
        "citation", "exact_source_title_or_citation", "source_title_or_citation", "exact_source_title", "title")]
    tokens = list(dict.fromkeys(match[1].strip() for citation in citations for match in re.finditer(
        r"\bISBN(?:-1[03])?\s*:?\s*([0-9Xx][0-9Xx\- ]{8,20}[0-9Xx])\b", citation, re.I)))
    if tokens:
        identities = {book_isbn(token) for token in tokens}
        return "; ".join(tokens), identities.pop() if len(identities) == 1 else ""
    try:
        value = isbn_from_material(material)
    except ValueError:
        return "", ""
    return value, book_isbn(value)


def author_surnames(value):
    segments = re.split(r"[,;&/]|\band\b", str(value or ""), flags=re.I)
    names = set()
    for segment in segments:
        words = [word for word in plain(segment).split() if len(word) > 1 and word not in {"et", "al", "jr", "editor", "editors"}]
        if words:
            names.add(words[-1])
    return names


def evidence_identity(row, material):
    return str(material.get("evidence_id") or row.get("evidence_id") or "")


def source_assignments(rows):
    assignments = {}
    for row in rows:
        for index, material in enumerate(row.get("materials", [])):
            if not isinstance(material, dict):
                continue
            key = (evidence_identity(row, material), index)
            if not key[0]:
                continue
            previous = assignments.get(key)
            if previous is not None and previous != material:
                raise ValueError("Ambiguous original evidence assignment")
            assignments[key] = material
    return assignments


def checked_resolution(record, assignments, checked_on):
    if not isinstance(record, dict):
        raise ValueError("Each ISBN resolution must be an object")
    eid = text(record.get("evidence_id"), "evidence_id")
    index = record.get("material_index")
    if type(index) is not int or index < 0 or (eid, index) not in assignments:
        raise ValueError("ISBN resolution requires a valid original evidence material_index")
    material = assignments[(eid, index)]
    if not is_book_material(material):
        raise ValueError("ISBN resolution must reference a book")
    for field, expected in (("course_book_title", material_title(material)),
                            ("course_citation", str(material.get("citation") or material_title(material)))):
        if record.get(field) != expected:
            raise ValueError("Stale ISBN resolution: " + field + " differs from the course source")
    for field in ("publisher_title", "publisher_authors", "publisher_edition", "format_name", "match_rationale"):
        text(record.get(field), field)
    if plain(record["publisher_title"]) != plain(record["course_book_title"]):
        text(record.get("title_match_rationale"), "title_match_rationale")
    authors = material.get("authors") or material.get("author")
    if authors and author_surnames(authors) != author_surnames(record["publisher_authors"]):
        text(record.get("author_match_rationale"), "author_match_rationale")
    checked_url(record.get("source_url"), PUBLISHER_DOMAINS)
    value = text(record.get("isbn"), "isbn")
    isbn = book_isbn(value)
    if not isbn:
        raise ValueError("Publisher ISBN checksum is invalid")
    raw, assigned_isbn = source_isbn(material)
    if raw and not assigned_isbn:
        raise ValueError("Invalid course-source ISBN cannot be resolved silently")
    if assigned_isbn and assigned_isbn != isbn:
        raise ValueError("Publisher ISBN conflicts with the course-source ISBN")
    status = record.get("match_status")
    if status not in {"edition-confirmed", "edition-unconfirmed"}:
        raise ValueError("Unknown ISBN match_status")
    if not authors and status == "edition-confirmed" and assigned_isbn != isbn:
        text(record.get("author_match_rationale"), "author_match_rationale")
    assigned_edition = edition_identity(material.get("edition"))
    if status == "edition-confirmed" and not (
            assigned_isbn == isbn or assigned_edition and assigned_edition == edition_identity(record["publisher_edition"])):
        raise ValueError("Edition confirmation requires the same stated edition or course-source ISBN")
    record_date = text(record.get("checked_on"), "checked_on")
    date.fromisoformat(record_date)
    if record_date != checked_on:
        raise ValueError("Resolution checked_on differs from its evidence file")
    return dict(record) | {"isbn": isbn}


def load_resolutions(folder, source_rows):
    assignments = source_assignments(source_rows)
    resolutions = {}
    for name in RESOLUTION_FILES:
        path = child(folder, name)
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("format") != FORMAT or not isinstance(payload.get("resolutions"), list):
            raise ValueError("Expected ISBN evidence format and resolutions list")
        checked_on = text(payload.get("checked_on"), "checked_on")
        date.fromisoformat(checked_on)
        for record in payload["resolutions"]:
            record = checked_resolution(record, assignments, checked_on)
            key = (record["evidence_id"], record["material_index"])
            if key in resolutions:
                raise ValueError("Duplicate ISBN resolution for an evidence assignment")
            resolutions[key] = record
    return resolutions


def resolution_lookup(resolutions):
    identities = {}
    for record in (resolutions or {}).values():
        identity = (record["evidence_id"], record["course_book_title"], record["course_citation"])
        if identity in identities and identities[identity] != record:
            raise ValueError("Ambiguous ISBN resolutions for the same source citation")
        identities[identity] = record
    return identities


def build_matches(source_rows, resolutions=None, *, university=None, course=None):
    institution = resolve_institution(university) if university else None
    if institution and not institution["in_scope"]:
        raise ValueError("Unknown university filter")
    lookup = resolution_lookup(resolutions)
    matches = []
    for source in source_rows:
        university_name = str(source.get("institution") or "")
        identity = resolve_institution(university_name)
        if not identity["in_scope"]:
            raise ValueError("Source university is outside the 12-university scope")
        course_code = str(source.get("code") or source.get("course_code") or "")
        if institution and institution["institution_id"] != identity["institution_id"]:
            continue
        if course and course_code.casefold() != course.casefold():
            continue
        for index, material in enumerate(source.get("materials", [])):
            if not isinstance(material, dict) or not is_learning_book(material):
                continue
            title = material_title(material)
            citation = str(material.get("citation") or title)
            eid = evidence_identity(source, material)
            resolution = lookup.get((eid, title, citation))
            raw, isbn = source_isbn(material)
            row = dict.fromkeys(FIELDS, "") | {
                "university": university_name, "course_key": source.get("course_key", ""),
                "course_code": course_code, "course_title": source.get("title") or source.get("course_title") or "",
                "book_title": title, "authors": material.get("authors") or material.get("author") or "",
                "assigned_edition": material.get("edition") or "", "assigned_isbn": isbn, "assigned_isbn_raw": raw,
                "matched_isbn": isbn, "isbn_status": "course-source-isbn" if isbn else "invalid-course-source-isbn" if raw else "missing",
                "assignment_role": material_assignment_role(material),
                "course_source_url": material.get("source_url") or source.get("source_url") or "",
                "source_year": material.get("source_year") or source.get("source_year") or "",
                "source_verification_status": material.get('source_verification_status') or '',
                "evidence_id": eid, "material_index": index, "course_citation": citation,
                "bibliography_verified": False, "current_adoption_verified": False, "knowledge_processed": False}
            origin = material.get("metadata_origin", {})
            if material.get("association_status") == "library-course-reserve-candidate" or origin.get("isbn") == "library-bibliography":
                library_isbn = book_isbn(str(material.get("isbn") or ""))
                row |= {"assigned_isbn": "", "assigned_isbn_raw": "", "matched_isbn": "", "candidate_isbn": library_isbn,
                        "assigned_edition": "", "library_edition": material.get("edition") or "",
                        "isbn_origin": "library-bibliography", "isbn_status": "library-reserve-candidate" if library_isbn else "missing",
                        "bibliographic_source_url": row["course_source_url"]}
            else:
                row["isbn_origin"] = "course-citation" if raw else "not-stated"
            if resolution:
                if raw and not isbn or isbn and isbn != resolution["isbn"]:
                    raise ValueError("Resolution conflicts with course-source ISBN")
                row |= {"bibliographic_source_url": resolution["source_url"], "format_name": resolution["format_name"],
                        "resolution_material_index": resolution["material_index"],
                        "publisher_title": resolution["publisher_title"], "publisher_authors": resolution["publisher_authors"],
                        "publisher_edition": resolution["publisher_edition"], "match_rationale": resolution["match_rationale"],
                        "title_match_rationale": resolution.get("title_match_rationale", ""),
                        "author_match_rationale": resolution.get("author_match_rationale", "")}
                if resolution["match_status"] == "edition-confirmed":
                    row |= {"matched_isbn": resolution["isbn"], "isbn_status": "publisher-edition-matched", "bibliography_verified": True}
                else:
                    row |= {"candidate_isbn": resolution["isbn"], "isbn_status": "publisher-edition-unconfirmed"}
            row['finding_aids'] = finding_aids(material)
            matches.append(row)
    return matches


def summarize(rows):
    statuses = Counter(row["isbn_status"] for row in rows)
    return {"mappings": len(rows), "course_keys": len({row["course_key"] for row in rows}),
            "course_source_isbn_rows": sum(bool(row["assigned_isbn"]) for row in rows),
            "publisher_edition_matched_rows": statuses["publisher-edition-matched"],
            "library_reserve_candidate_rows": statuses["library-reserve-candidate"],
            "candidate_isbn_rows": sum(bool(row["candidate_isbn"]) for row in rows),
            "missing_isbn_rows": statuses["missing"], "invalid_course_source_isbn_rows": statuses["invalid-course-source-isbn"],
            "finding_aid_isbn_rows": sum(any(finding.get("isbn") for finding in row.get("finding_aids", [])) for row in rows),
            "rows_without_any_isbn": sum(not (row["assigned_isbn"] or row["matched_isbn"] or row["candidate_isbn"] or
                any(finding.get("isbn") for finding in row.get("finding_aids", []))) for row in rows),
            "rows_without_supported_isbn": sum(not (row["assigned_isbn"] or row["matched_isbn"] or row["candidate_isbn"] or
                any(finding.get("isbn") and finding.get("basis") not in {"library-title-only-candidate", "library-work-only-candidate"}
                    for finding in row.get("finding_aids", []))) for row in rows)}


def render(rows, output_format):
    if output_format == "json":
        return json.dumps({"format": "topclass-course-book-matches-v1", "summary": summarize(rows), "matches": rows},
                          ensure_ascii=False, indent=2) + "\n"
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: csv_value(row[key]) for key in FIELDS} for row in rows)
    return stream.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Match documented course book titles to source and publisher ISBN evidence offline.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--all-saved", action="store_true", help="Read all saved consolidated book mentions instead of the bounded reading supplement.")
    parser.add_argument("--university", help="Exact university name or known alias.")
    parser.add_argument("--course", help="Exact course code, case-insensitive.")
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    parser.add_argument("--out", type=Path, help="Write a new file without overwriting an existing path; otherwise print to standard output.")
    args = parser.parse_args(argv)
    if args.out and (args.out.exists() or args.out.is_symlink()):
        raise FileExistsError("Output already exists: " + str(args.out))
    bounded = load_readings(args.root)
    resolutions = load_resolutions(child(args.root, FOLDER), bounded)
    readings = load_readings(args.root, all_saved=True) if args.all_saved else bounded
    rows = build_matches(readings, resolutions, university=args.university, course=args.course)
    output = render(rows, args.format)
    if args.out:
        with args.out.open("x", encoding="utf-8", newline="") as handle:
            handle.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print("Error: " + str(error), file=sys.stderr)
        raise SystemExit(1)
