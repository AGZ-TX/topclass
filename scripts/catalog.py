#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import lzma
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_catalog() -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    folder = ROOT / "data"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    chunks = []
    for index, part in enumerate(manifest["parts"]):
        name = part["file"]
        require(name == f"catalog.xz.{index:03d}", "Unexpected archive part order/name")
        chunk = (folder / name).read_bytes()
        require(len(chunk) == part["bytes"], f"Size mismatch: {name}")
        require(digest(chunk) == part["sha256"], f"SHA-256 mismatch: {name}")
        chunks.append(chunk)
    compressed = b"".join(chunks)
    require(digest(compressed) == manifest["compressed_sha256"], "Archive hash mismatch")
    limit = int(manifest["decompressed_bytes"])
    require(0 < limit <= 20_000_000, "Unexpected decompressed size")
    decoder = lzma.LZMADecompressor(memlimit=256 * 1024 * 1024)
    raw = decoder.decompress(compressed, max_length=limit + 1)
    require(decoder.eof and not decoder.unused_data, "Incomplete or trailing XZ data")
    require(len(raw) == limit, "Decompressed size mismatch")
    require(digest(raw) == manifest["decompressed_sha256"], "JSON hash mismatch")
    data = json.loads(raw)
    validate(data, manifest)
    return data, raw, manifest


def validate(data: dict[str, Any], manifest: dict[str, Any]) -> None:
    for name, count in manifest["counts"].items():
        require(re.fullmatch(r"[a-z_]+", name) is not None, "Invalid collection name")
        require(isinstance(data.get(name), list), f"Missing collection: {name}")
        require(len(data[name]) == count, f"Count mismatch: {name}")
        require(all(isinstance(row, dict) for row in data[name]), f"Invalid rows: {name}")
    courses = {row["course_id"] for row in data["courses"]}
    books = {row["book_id"] for row in data["books"]}
    sources = {row["source_id"] for row in data["sources"]}
    works = {row["work_id"] for row in data["book_works"]}
    require(courses == {f"C{i:03d}" for i in range(1, 367)}, "Course IDs changed")
    require(len(books) == len(data["books"]), "Duplicate book IDs")
    require(len(sources) == len(data["sources"]), "Duplicate source IDs")
    require(len(works) == len(data["book_works"]), "Duplicate work IDs")
    for collection in manifest["counts"]:
        for row in data[collection]:
            for field, valid in (("course_ids", courses), ("assignment_ids", books)):
                value = row.get(field)
                if isinstance(value, list):
                    require(set(value) <= valid, f"Broken {field} in {collection}")
            value = row.get("source_id")
            if value:
                require(value in sources, f"Unknown source {value} in {collection}")
    for row in data["courses"] + data["book_coverage"]:
        refs = set(re.findall(r"\bB\d{3}\b", str(row.get("book_ids", ""))))
        require(refs <= books, f"Broken book link: {row['course_id']}")
    for row in data["books"]:
        require(row["work_id"] in works, f"Broken work link: {row['book_id']}")
    for row in data["reading_map"]:
        require(row["book_id"] in books, f"Broken reading link: {row['reading_id']}")
    coverage = data["book_coverage"]
    require({r["course_id"] for r in coverage} == courses, "Coverage IDs mismatch")
    statuses = Counter(r["book_research_status"] for r in coverage)
    require(dict(statuses) == manifest["coverage_counts"], "Coverage counts mismatch")


def csv_cell(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return "" if value is None else value


def export(data: dict[str, Any], raw: bytes, manifest: dict[str, Any], out: Path) -> None:
    out = out.resolve()
    require(not out.exists() or (out.is_dir() and not any(out.iterdir())),
            f"Choose a new or empty output directory: {out}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "curriculum_atlas.json").write_bytes(raw)
    (out / "metadata.json").write_text(json.dumps(data["metadata"], indent=2,
                                                ensure_ascii=False) + "\n", encoding="utf-8")
    for name in manifest["counts"]:
        rows = data[name]
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with (out / f"{name}.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: csv_cell(row.get(key)) for key in fields} for row in rows)
    print(f"Exported exact JSON, metadata, and {len(manifest['counts'])} CSVs to {out}")


def recurrence(data: dict[str, Any]) -> dict[str, Any]:
    courses = {row["course_id"]: row for row in data["courses"]}

    def canonical_isbn(row: dict[str, Any]) -> str:
        digits = re.sub(r"[^0-9Xx]", "", row.get("isbn") or "").upper()
        validation = str(row.get("isbn_validation", "")).casefold()
        if not digits or not (validation.startswith("valid") or "checksum valid" in validation):
            return ""
        if len(digits) == 10:
            body = "978" + digits[:9]
            check = (10 - sum(int(char) * (1 if index % 2 == 0 else 3)
                              for index, char in enumerate(body)) % 10) % 10
            return body + str(check)
        return digits if len(digits) == 13 else ""

    works: dict[str, list[dict[str, Any]]] = {}
    for row in data["books"]:
        works.setdefault(row["work_id"], []).append(row)

    repeated_works = []
    for work_id, rows in works.items():
        course_ids = sorted({course_id for row in rows for course_id in row["course_ids"]})
        linked_courses = [courses[course_id] for course_id in course_ids]
        schools = sorted({course["school"] for course in linked_courses})
        roles = Counter(row["assignment_category"] for row in rows)
        editions: dict[str, list[str]] = {}
        edition_isbns: dict[str, list[str]] = {}
        course_identities: dict[tuple[str, str], list[dict[str, str]]] = {}
        course_reference_counts: Counter[str] = Counter()
        for row in rows:
            edition = " ".join((row.get("assigned_edition") or "Not specified").split())
            editions.setdefault(edition, []).append(row["book_id"])
            isbn = canonical_isbn(row)
            if isbn:
                edition_isbns.setdefault(isbn, []).append(row["book_id"])
            for course_id in row["course_ids"]:
                course_reference_counts[course_id] += 1
                course = courses[course_id]
                code = course["course_code"].split("/", 1)[0].strip()
                if not code or code.casefold() in {"no code listed", "n/a", "unknown"}:
                    code = course_id
                identity = (course["school"], re.sub(r"\s+", "", code).casefold())
                course_identities.setdefault(identity, []).append({
                    "course_id": course_id, "course_code": course["course_code"]
                })
        if len(rows) < 2:
            continue
        overlap_course_ids = sorted(course_id for course_id, count in course_reference_counts.items()
                                     if count > 1)
        if len(course_identities) > 1:
            recurrence_kind = "cross_course"
        elif overlap_course_ids:
            recurrence_kind = "same_offering_multiple_references"
        else:
            recurrence_kind = "same_course_across_catalog_records"

        repeated_works.append({
            "work_id": work_id,
            "title": rows[0]["title"],
            "authors": rows[0].get("authors", ""),
            "reference_records": len(rows),
            "distinct_course_records": len(course_ids),
            "distinct_course_identities": len(course_identities),
            "recurrence_kind": recurrence_kind,
            "distinct_courses": [
                {"course_id": course["course_id"], "school": course["school"],
                 "course_code": course["course_code"], "title": course["title"]}
                for course in linked_courses
            ],
            "course_identity_groups": [
                {"school": school, "primary_code": code,
                 "course_ids": sorted({item["course_id"] for item in members}),
                 "course_codes": sorted({item["course_code"] for item in members})}
                for (school, code), members in sorted(course_identities.items())
            ],
            "schools": schools,
            "assignment_categories": dict(sorted(roles.items())),
            "edition_strings": [
                {"edition": edition, "reference_ids": sorted(ids)}
                for edition, ids in sorted(editions.items())
            ],
            "same_isbn_candidates": [
                {"isbn": isbn, "reference_ids": sorted(ids),
                 "titles": sorted({row["title"] for row in rows if row["book_id"] in ids}),
                 "authors": sorted({row.get("authors", "") for row in rows if row["book_id"] in ids}),
                 "status": "candidate; checksum validity and matching titles do not establish bibliographic identity"}
                for isbn, ids in sorted(edition_isbns.items()) if len(ids) > 1
            ],
            "source_urls": sorted({row["source_url"] for row in rows if row.get("source_url")}),
            "same_course_overlap_candidates": overlap_course_ids,
        })
    repeated_works.sort(key=lambda row: (
        -row["distinct_course_identities"], -row["reference_records"], row["title"].casefold()
    ))

    topic_rows: dict[str, list[dict[str, Any]]] = {}
    for row in data["reading_map"]:
        topic = " ".join((row.get("topic") or "").split())
        if topic:
            topic_rows.setdefault(topic.casefold(), []).append(row)
    repeated_topics = [
        {"topic": rows[0]["topic"], "reading_records": len(rows),
         "course_ids": sorted({course_id for row in rows
                               for course_id in (row.get("course_ids") or "").split(",") if course_id}),
         "source_urls": sorted({row["source_url"] for row in rows if row.get("source_url")})}
        for rows in topic_rows.values() if len(rows) > 1
    ]
    repeated_topics.sort(key=lambda row: (-row["reading_records"], row["topic"].casefold()))

    return {
        "catalog_version": data["metadata"]["release"],
        "method": "Saved work IDs; school plus primary course code for course identity; checksum-valid ISBN matches as deduplication candidates; and case-insensitive exact reading-topic labels.",
        "work_caveat": "Work-level grouping does not identify an exact edition or prove every listed item was assigned or read. Cross-listed codes are represented by their first code; course records and original aliases remain visible.",
        "knowledge_caveat": "No supplied book corpus has been processed. Repeated reading-map labels are source-scope metadata, not proof that knowledge was extracted or repeatedly used.",
        "repeated_works": repeated_works,
        "repeated_reading_topics": repeated_topics,
        "reading_topic_label_count": len(topic_rows),
        "knowledge_corpus_processed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read, verify, and export the preserved catalog. Python 3.10+, standard library only."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="Check preservation hashes, counts, and record links")
    sub.add_parser("status", help="Show saved counts, coverage, and limitations")
    listing = sub.add_parser("courses", help="List/filter existing courses, not rank them")
    listing.add_argument("--school", default="", help="Case-insensitive substring")
    listing.add_argument("--domain", default="", help="Case-insensitive substring")
    saving = sub.add_parser("export", help="Export the snapshot into a new/empty folder")
    saving.add_argument("--out", type=Path, default=ROOT / "exports" / "v0.4")
    sub.add_parser("recurrence", help="Report repeated material links and exact reading-topic labels")
    args = parser.parse_args()
    try:
        data, raw, manifest = load_catalog()
        if args.command == "verify":
            print("PASS: exact snapshot hashes, counts, IDs, and checked links.")
            print("This verifies preservation, not source accuracy or agent performance.")
        elif args.command == "status":
            print(json.dumps({key: manifest[key] for key in
                              ("catalog_version", "source_checked_on", "counts",
                               "coverage_counts", "not_claimed")}, indent=2))
        elif args.command == "courses":
            rows = [r for r in data["courses"]
                    if args.school.casefold() in r["school"].casefold()
                    and args.domain.casefold() in r["domain"].casefold()]
            for row in rows:
                print(f"{row['course_id']} | {row['school']} | {row['course_code']} | "
                      f"{row['title']} | books: {row['book_ids']}")
            print(f"{len(rows)} matching saved records")
        elif args.command == "export":
            export(data, raw, manifest, args.out)
        elif args.command == "recurrence":
            print(json.dumps(recurrence(data), indent=2, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, lzma.LZMAError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
