#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
from pathlib import Path

try:
    from .book_matches import book_isbn, build_matches, edition_identity, load_readings, load_resolutions
    from .catalog_identity import resolve_institution
    from .course_map import csv_value
    from .reading_enrichment import FOLDER, ROOT, child
    from .universe_html import render_html
except ImportError:
    from book_matches import book_isbn, build_matches, edition_identity, load_readings, load_resolutions
    from catalog_identity import resolve_institution
    from course_map import csv_value
    from reading_enrichment import FOLDER, ROOT, child
    from universe_html import render_html


FORMAT = "topclass-university-courses-books-v1"
CSV_FIELDS = ("university_id", "university", "course_id", "course_code", "course_title", "school",
              "book_id", "book_title", "authors", "assigned_editions", "publisher_editions", "library_editions",
              "assigned_isbns", "publisher_matched_isbns", "publisher_candidate_isbns", "library_candidate_isbns",
              "isbn_origins", "isbn_statuses", "assignment_roles", "evidence", "finding_aids", "source_records")
UNKNOWN_IDENTITIES = {"", "unknown", "n/a", "na", "not stated", "no code listed", "unresolved"}
SOURCE_FIELDS = ("course_key", "baseline_course_id", "institution", "code", "course_code", "title", "course_title",
                 "school", "status", "evidence_statuses", "sources", "source_records", "gaps", "primary_academic_area",
                 "classification_status", "review_reasons", "primary_candidates", "primary_resolution",
                 "classification_method", "classification_version", "taxonomy_version", "classification_evidence",
                 "original_primary_academic_area", "rule_classification", "model_classification",
                 "expertise_tags", "primary_expertise", "original_expertise_tags", "discovery_academic_areas", "facet_classification",
                 "literal_facet_classification", "public_subject_evidence", "bibliographic_subject_evidence", "source_subject_context_evidence")


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix, identity):
    return prefix + ":" + hashlib.sha256(serialized(identity).encode("utf-8")).hexdigest()[:24]


def course_code(value, university):
    result = normalized(value)
    patterns = {"mit": r"^(?:mit\s+)?(\d+[a-z]*\.[a-z]*\d+[a-z0-9]*)$",
                "carnegie-mellon": r"^(?:cmu\s+)?(\d{2})[- ]?(\d{3}[a-z]?)$"}
    if university in patterns:
        match = re.fullmatch(patterns[university], result)
        if match:
            return match[1] if university == "mit" else match[1] + "-" + match[2]
    return result


def course_school(row, institution):
    school = str(row.get("school") or institution.get("school_name") or "")
    if not school:
        records = list(row.get("source_records") or [])
        for evidence in row.get("source_evidence_rows") or []:
            if isinstance(evidence, dict):
                record = evidence.get("source_record")
                if isinstance(record, dict):
                    records.append(record)
        schools = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            for field in ("school", "college", "school_name", "college_name"):
                value = str(record.get(field) or "").strip()
                if value:
                    schools.setdefault(normalized(value), set()).add(value)
        school = " | ".join(sorted(min(labels) for labels in schools.values()))
    resolved = resolve_institution(school)
    if resolved["in_scope"] and resolved["institution_id"] == institution["institution_id"] and not resolved["school_id"]:
        return ""
    return school


def course_identity(row, institution):
    code = course_code(row.get("code") or row.get("course_code"), institution["institution_id"])
    title = normalized(row.get("title") or row.get("course_title"))
    school = normalized(course_school(row, institution))
    fallback = str(row.get("course_key") or stable_id("source", row)) if code in UNKNOWN_IDENTITIES or title in UNKNOWN_IDENTITIES else ""
    return institution["institution_id"], code, title, school, fallback


def display_title(row):
    title = row["book_title"]
    assigned = row["assigned_isbn"]
    if not assigned or row["isbn_origin"] == "library-bibliography" or normalized(row["authors"]) in UNKNOWN_IDENTITIES:
        return title
    parts = title.rsplit(" / ", 2)
    if len(parts) != 3 or not parts[0].strip() or normalized(parts[1]) != normalized(row["authors"]):
        return title
    suffix = re.fullmatch(r"ISBN(?:-1[03])?\s*:?\s*([0-9Xx][0-9Xx\- ]{8,20}[0-9Xx])", parts[2].strip(), re.I)
    if suffix and book_isbn(suffix[1]) == assigned:
        return parts[0].strip()
    return title


def book_identity(row):
    isbn = row["matched_isbn"] or row["candidate_isbn"]
    edition = row["assigned_edition"] or row["library_edition"] or row["publisher_edition"]
    authors = normalized(row["authors"])
    if authors in UNKNOWN_IDENTITIES:
        authors = ""
    title = normalized(display_title(row))
    incomplete = authors in UNKNOWN_IDENTITIES or (not isbn and normalized(edition) in UNKNOWN_IDENTITIES)
    unresolved = (normalized(row["course_citation"]), row["assigned_isbn_raw"], row["isbn_origin"],
                  row["isbn_status"]) if incomplete or row["isbn_status"] == "invalid-course-source-isbn" else ()
    edition_key = "" if normalized(edition) in UNKNOWN_IDENTITIES else edition_identity(edition)
    candidate_kind = "publisher-edition-unconfirmed" if row["isbn_status"] == "publisher-edition-unconfirmed" else ""
    return isbn, title, authors, edition_key, unresolved, candidate_kind


def values(rows, field, predicate=None):
    metadata = field in {"authors", "assigned_edition", "publisher_edition", "library_edition"}
    return sorted({str(row[field]) for row in rows if row[field] and (not metadata or normalized(row[field]) not in UNKNOWN_IDENTITIES)
                   and (predicate is None or predicate(row))},
                  key=lambda value: (normalized(value), value))


def book_view(identity, rows, course_id):
    evidence = sorted(rows, key=serialized)
    titles = sorted({display_title(row) for row in rows}, key=lambda value: (normalized(value), value))
    derivations = {serialized({"source_title": row["book_title"], "display_title": display_title(row),
                              "method": "structured-course-citation"}) for row in rows if display_title(row) != row["book_title"]}
    return {"id": stable_id("book", (course_id, identity)), "title": titles[0], "titles": titles,
            "title_derivations": [json.loads(value) for value in sorted(derivations)], "authors": values(rows, "authors"),
            "assigned_editions": values(rows, "assigned_edition"), "publisher_editions": values(rows, "publisher_edition"),
            "library_editions": values(rows, "library_edition"), "assigned_isbns": values(rows, "assigned_isbn"),
            "publisher_matched_isbns": values(rows, "matched_isbn", lambda row: row["isbn_status"] == "publisher-edition-matched"),
            "publisher_candidate_isbns": values(rows, "candidate_isbn", lambda row: row["isbn_status"] == "publisher-edition-unconfirmed"),
            "library_candidate_isbns": values(rows, "candidate_isbn", lambda row: row["isbn_origin"] == "library-bibliography"),
            "isbn_origins": values(rows, "isbn_origin"), "isbn_statuses": values(rows, "isbn_status"),
            "assignment_roles": values(rows, "assignment_role"), "evidence": evidence,
            "finding_aids": [json.loads(value) for value in sorted({serialized(finding) for row in rows for finding in row.get("finding_aids", [])})]}


def counts(courses):
    return {"courses": len(courses), "courses_with_books": sum(bool(course["books"]) for course in courses),
            "course_book_links": sum(len(course["books"]) for course in courses)}


def explicitly_nondegree(row):
    records = [row] + [record for record in row.get("source_records") or [] if isinstance(record, dict)]
    for evidence in row.get("source_evidence_rows") or []:
        if isinstance(evidence, dict) and isinstance(evidence.get("source_record"), dict):
            records.append(evidence["source_record"])
    institution = resolve_institution(row.get("institution"))
    for record in records:
        if normalized(record.get("degree_status")) == "nondegree":
            return True
        if institution["institution_id"] == "stanford" and any(
                normalized(record.get(field)) == "stanford continuing studies"
                for field in ("school", "college", "school_name", "college_name")):
            return True
    return False


def compact_specialty_tags(tags):
    fields = ("id", "label", "basis", "human_reviewed", "confidence", "model", "state_fingerprint", "source_fingerprint", "specialty_id")
    return [{field: tag[field] for field in fields if field in tag and isinstance(tag[field], (str, bool, int, float))
             and (not isinstance(tag[field], str) or len(tag[field]) <= 200)}
            for tag in tags if isinstance(tag, dict) and isinstance(tag.get("id"), str)
            and len(tag["id"]) <= 100
            and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", tag["id"])
            and isinstance(tag.get("label"), str) and 0 < len(tag["label"]) <= 200]


def compact_source(row):
    result = {key: row[key] for key in SOURCE_FIELDS if key in row
              and key not in {"expertise_tags", "original_expertise_tags", "discovery_academic_areas", "facet_classification",
                              "literal_facet_classification", "public_subject_evidence", "bibliographic_subject_evidence", "source_subject_context_evidence"}}
    for field in ("expertise_tags", "original_expertise_tags", "discovery_academic_areas"):
        tags = compact_specialty_tags(row.get(field) or [])
        if tags or field == "discovery_academic_areas" and field in row:
            result[field] = tags
    if "discovery_academic_areas" in row:
        result["discovery_academic_areas"] = compact_discovery_areas(row["discovery_academic_areas"])
    model = row.get("facet_classification")
    if isinstance(model, dict):
        fields = ("course_key", "question_version", "source_fingerprint", "reading_fingerprint", "taxonomy_fingerprint",
                  "rubric_fingerprint", "anchor_fingerprint", "resolved_model", "supported_academic_area_ids",
                  "classification_status", "human_reviewed", "identity_warning", "current_adoption_verified", "task_fit_verified",
                  "provider_receipt")
        result["facet_classification"] = {field: model[field] for field in fields if field in model}
        result["facet_classification"]["accepted_facets"] = compact_discovery_areas(model.get("accepted_facets", []))
    literal = row.get("literal_facet_classification")
    if isinstance(literal, dict):
        result["literal_facet_classification"] = {key: literal[key] for key in (
            "grounding_version", "source_fingerprint", "reading_fingerprint", "taxonomy_fingerprint", "human_reviewed") if key in literal}
        result["literal_facet_classification"]["facets"] = compact_discovery_areas(literal.get("facets", []))
    public = row.get("public_subject_evidence")
    if isinstance(public, dict):
        result["public_subject_evidence"] = {key: public[key] for key in (
            "source_course_key", "source_url", "source_year", "source_term", "identity_binding", "human_reviewed",
            "current_adoption_verified", "task_fit_verified") if key in public}
        result["public_subject_evidence"]["facets"] = compact_discovery_areas(public.get("facets", []))
    for name in ("bibliographic_subject_evidence", "source_subject_context_evidence"):
        proof = row.get(name)
        if isinstance(proof, dict):
            result[name] = {key: proof[key] for key in ("policy_version", "human_reviewed", "current_adoption_verified", "task_fit_verified", "limit") if key in proof}
            result[name]["facets"] = compact_discovery_areas(proof.get("facets", []))
    return result


def compact_discovery_areas(areas):
    result = []
    anchor_fields = ("id", "kind", "field", "quote", "reading_index", "probability", "source_urls", "source_year",
                     "source_course_versions", "citation", "course_key", "book_id", "covered_group_id", "assignment_role",
                     "assignment_roles", "source_url", "matched_field", "prefix", "locator", "source_term",
                     "bibliographic_source_url", "assigned_isbn", "bibliographic_candidate_isbn", "course_citation", "reviewer", "reviewed_on", "record_fingerprint", "source_text_fingerprint")
    for area in areas:
        compact = compact_specialty_tags([area])
        if not compact:
            continue
        facet = compact[0]
        for field in ("grounding_version", "grounding_basis", "reading_fingerprint", "taxonomy_fingerprint", "rubric_fingerprint",
                      "source_status", "catalog_metadata_review", "edition_status", "context_version"):
            if isinstance(area.get(field), str) and len(area[field]) <= 200:
                facet[field] = area[field]
        for field in ("matched_phrases", "source_evidence_fields", "context_bases", "record_fingerprints"):
            if isinstance(area.get(field), list):
                facet[field] = [value for value in area[field] if isinstance(value, str) and len(value) <= 200]
        if isinstance(area.get("anchor_candidates"), list):
            facet["anchor_candidates"] = [{field: anchor[field] for field in anchor_fields if field in anchor}
                for anchor in area["anchor_candidates"] if isinstance(anchor, dict) and anchor.get("kind") in {"course", "reading"}
                and isinstance(anchor.get("quote"), str) and len(anchor["quote"]) <= 2000]
            for original, projected in zip((anchor for anchor in area["anchor_candidates"] if isinstance(anchor, dict)
                    and anchor.get("kind") in {"course", "reading"} and isinstance(anchor.get("quote"), str)
                    and len(anchor["quote"]) <= 2000), facet["anchor_candidates"]):
                if isinstance(original.get("lexical_proofs"), list):
                    projected["lexical_proofs"] = [{key: proof[key] for key in (
                        "taxonomy_phrase", "matched_text", "field", "method", "identity_source")
                        if isinstance(proof.get(key), str) and len(proof[key]) <= 2000}
                        for proof in original["lexical_proofs"] if isinstance(proof, dict)]
                if isinstance(original.get("phrase_proofs"), list):
                    projected["phrase_proofs"] = [{key: proof[key] for key in ("academic_area_id", "phrase", "matched_text", "method", "match")
                        if isinstance(proof.get(key), str) and len(proof[key]) <= 2000}
                        for proof in original["phrase_proofs"] if isinstance(proof, dict)]
        result.append(facet)
    return result


def build_universe(source_rows, resolutions=None, *, university=None, course=None, with_books=False):
    selected = resolve_institution(university) if university else None
    if selected and not selected["in_scope"]:
        raise ValueError("Unknown university filter")
    groups = {}
    for row in source_rows:
        institution = resolve_institution(row.get("institution"))
        if not institution["in_scope"]:
            raise ValueError("Source university is outside the 12-university scope")
        if selected and selected["institution_id"] != institution["institution_id"]:
            continue
        if course and normalized(row.get("code") or row.get("course_code")) != normalized(course):
            continue
        identity = course_identity(row, institution)
        group = groups.setdefault(identity, {"institution": institution, "sources": [], "schools": set(), "books": {}})
        group["sources"].append(compact_source(row))
        group["schools"].add(course_school(row, institution))
        for match in build_matches([row], resolutions):
            group["books"].setdefault(book_identity(match), []).append(match)
    universities = {}
    for identity, group in groups.items():
        identifier = stable_id("course", identity)
        books = [book_view(book_key, matches, identifier) for book_key, matches in group["books"].items()]
        books.sort(key=lambda value: (normalized(value["title"]), value["id"]))
        if with_books and not books:
            continue
        sources = sorted(group["sources"], key=serialized)
        codes = sorted({str(row.get("code") or row.get("course_code") or "") for row in sources})
        titles = sorted({str(row.get("title") or row.get("course_title") or "") for row in sources})
        institution = group["institution"]
        entry = universities.setdefault(institution["institution_id"], {
            "id": institution["institution_id"], "name": institution["institution_name"], "courses": []})
        entry["courses"].append({"id": identifier, "code": codes[0], "title": titles[0],
                                 "school": min(group["schools"]), "books": books,
                                 "source_records": sources})
    result = sorted(universities.values(), key=lambda entry: entry["id"])
    for entry in result:
        entry["courses"].sort(key=lambda value: (normalized(value["code"]), normalized(value["title"]), value["id"]))
        entry["summary"] = counts(entry["courses"])
    return {"format": FORMAT, "scope": {"with_books_only": with_books, "university_filter": university,
                                        "course_filter": course, "source": "full-saved-materials-ledger",
                                        "source_metadata": "compact-ledger-references",
                                        "source_metadata_lookup": "course_key in original saved materials ledger"},
            "summary": {"universities": len(result), **counts([course for entry in result for course in entry["courses"]])},
            "universities": result}


def render(payload, output_format, *, summary_only=False):
    if output_format == "html":
        if summary_only:
            raise ValueError("--summary requires JSON output")
        return render_html(payload)
    if output_format == "json":
        result = payload if not summary_only else {**payload, "universities": [
            {key: value for key, value in entry.items() if key != "courses"} for entry in payload["universities"]]}
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if summary_only:
        raise ValueError("--summary requires JSON output")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for university in payload["universities"]:
        for course in university["courses"]:
            for book in course["books"] or [{}]:
                row = {"university_id": university["id"], "university": university["name"], "course_id": course["id"],
                       "course_code": course["code"], "course_title": course["title"], "school": course["school"],
                       "book_id": book.get("id", ""), "book_title": book.get("title", ""),
                       "source_records": serialized(course["source_records"])}
                for field in CSV_FIELDS[8:-1]:
                    row[field] = serialized(book.get(field, []))
                writer.writerow({field: csv_value(value) for field, value in row.items()})
    return stream.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show the saved university → courses → books map offline.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--university", help="Exact university name or known alias.")
    parser.add_argument("--course", help="Exact saved course code, case-insensitive.")
    coverage = parser.add_mutually_exclusive_group()
    coverage.add_argument("--with-books", action="store_true", help="Only show courses with documented named books (the default).")
    coverage.add_argument("--include-unmatched", action="store_true", help="Include unmatched catalog entries for gap research, not usable coverage.")
    parser.add_argument("--include-nondegree", action="store_true", help="Also include explicitly nondegree courses such as Stanford Continuing Studies.")
    parser.add_argument("--summary", action="store_true", help="Show map counts without course and book details; JSON only.")
    parser.add_argument("--format", choices=("json", "csv", "html"), default="json")
    parser.add_argument("--out", type=Path, help="Write a new file without overwriting an existing path.")
    args = parser.parse_args(argv)
    if args.out and (args.out.exists() or args.out.is_symlink()):
        raise FileExistsError("Output already exists: " + str(args.out))
    if args.summary and args.format != "json":
        raise ValueError("--summary requires JSON output")
    bounded = load_readings(args.root)
    resolutions = load_resolutions(child(args.root, FOLDER), bounded)
    readings = load_readings(args.root, all_saved=True)
    if not args.include_nondegree:
        readings = [row for row in readings if not explicitly_nondegree(row)]
    payload = build_universe(readings, resolutions, university=args.university, course=args.course,
                             with_books=not args.include_unmatched)
    payload["scope"]["degree_scope"] = "all-saved-scopes" if args.include_nondegree else "excludes-explicit-nondegree; unknown scope retained"
    output = render(payload, args.format, summary_only=args.summary)
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
