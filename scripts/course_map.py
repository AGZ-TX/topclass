#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import lzma
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from .catalog_identity import resolve_institution
    from .classify_courses import bind_merged_fingerprints
    from .jev_classification_overlay import apply_record, load_overlay, load_discovery_supplements, apply_discovery_supplements
except ImportError:
    from catalog_identity import resolve_institution
    from classify_courses import bind_merged_fingerprints
    from jev_classification_overlay import apply_record, load_overlay, load_discovery_supplements, apply_discovery_supplements

ROOT = Path(__file__).resolve().parents[1]
DATASETS = (
    ("course-inventory-2026-27", "classified-2026-27"),
    ("mit-catalog-2026-27", "classified-mit-2026-27"),
    ("cmu-catalog-2026-27", "classified-cmu-2026-27"),
    ("cornell-roster-2026-27", "classified-cornell-fa26"),
    ("cornell-catalog-2026-27", "classified-cornell-catalog-2026-27"),
    ("penn-catalog-2026-27", "classified-penn-2026-27"),
    ("yale-catalog-2026-27", "classified-yale-2026-27"),
    ("harvard-offerings-2026-27", "classified-harvard-2026-27"),
    ("columbia-engineering-2026-27", "classified-columbia-2026-27"),
    ("columbia-other-schools-catalog-2026-27", "classified-columbia-other-schools-2026-27"),
    ("columbia-public-school-course-catalog-2026-27", "classified-columbia-public-schools-2026-27"),
    ("brown-cab-2026-27", "classified-brown-2026-27"),
    ("dartmouth-catalog-2026-27", "classified-dartmouth-2026-27"),
    ("dartmouth-fall-2026", "classified-dartmouth-fa26"),
    ("dartmouth-tuck-catalog-2026-27", "classified-dartmouth-tuck-2026-27"),
    ("dartmouth-geisel-md-2026-27", "classified-dartmouth-geisel-md-2026-27"),
    ("princeton-cs-fall-2026", "classified-princeton-cs-fa26"),
    ("princeton-fall-2026-ecampus-term-only", "classified-princeton-ecampus-fall-2026-term-only"),
    ("princeton-ua-catalog-2026-27", "classified-princeton-ua-2026-27"),
    ("princeton-graduate-catalog-2026-27", "classified-princeton-grad-2026-27"),
    ("wcm-phs-course-catalog-undated", "classified-wcm-phs-course-catalog-undated"),
    ("wcm-pa-clinical-year-2026-27", "classified-wcm-pa-clinical-year-2026-27"),
)


def read_payload(folder: Path, entry: dict[str, Any], collection: str) -> dict[str, Any]:
    path = folder / entry["file"]
    compressed = path.read_bytes()
    if hashlib.sha256(compressed).hexdigest() != entry["compressed_sha256"]:
        raise ValueError(f"SHA-256 mismatch: {path}")
    value = json.loads(lzma.decompress(compressed))
    rows = value.get(collection)
    if not isinstance(rows, list) or len(rows) != entry["records"]:
        raise ValueError(f"Record-count mismatch: {path}")
    return value


def load_course_map(root: Path = ROOT / "data" / "expansion",
                    datasets: Iterable[tuple[str, str]] | None = None, *,
                    allow_partial: bool = False,
                    reviews: list[dict[str, Any]] | None = None,
                    include_materials: bool = True) -> list[dict[str, Any]]:
    if datasets is None:
        index_path = root / "catalog-datasets-2026-27.json"
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            datasets = ((item["inventory"], item["classification_index"])
                        for item in index["dataset_pairs"])
        else:
            datasets = DATASETS
    by_key: dict[str, dict[str, Any]] = {}
    for inventory_name, classified_name in datasets:
        inventory_dir, classified_dir = root / inventory_name, root / classified_name
        if not inventory_dir.exists() or not classified_dir.exists():
            if allow_partial:
                continue
            raise ValueError(f"Missing dataset: {inventory_name}/{classified_name}; use allow_partial explicitly for a partial map")
        inventory_manifest = json.loads((inventory_dir / "manifest.json").read_text(encoding="utf-8"))
        class_manifest = json.loads((classified_dir / "manifest.json").read_text(encoding="utf-8"))
        taxonomy_path = root / "expertise-taxonomy-v1.json"
        if taxonomy_path.exists():
            version = json.loads(taxonomy_path.read_text(encoding="utf-8"))["version"]
            if class_manifest.get("classification_version") != version:
                raise ValueError(f"Classification taxonomy version mismatch: {classified_name}")
        inventory_entries = [item for item in inventory_manifest["files"]
                             if item.get("collection", "courses") == "courses"]
        class_entries = {item["file"]: item for item in class_manifest["files"]}
        if set(class_entries) != {item["file"] for item in inventory_entries}:
            raise ValueError(f"Classification files do not match {inventory_name}")
        for entry in inventory_entries:
            source = read_payload(inventory_dir, entry, "courses")
            classified_entry = class_entries[entry["file"]]
            classified = read_payload(classified_dir, classified_entry, "classifications")
            labels = {row["course_key"]: row for row in classified["classifications"]}
            if len(labels) != len(classified["classifications"]):
                raise ValueError(f"Duplicate course key in {classified_dir / entry['file']}")
            if {row["course_key"] for row in source["courses"]} != set(labels):
                raise ValueError(f"Course/classification keys differ in {inventory_name}/{entry['file']}")
            for course in source["courses"]:
                record = {**course, **labels[course["course_key"]],
                          "inventory": inventory_name,
                          "classification_index": classified_name}
                if not record.get("institution"):
                    record["institution"] = source.get("institution") or entry.get("institution", "")
                canonical = resolve_institution(record["institution"])
                record["institution_id"] = canonical["institution_id"]
                record["institution_name"] = canonical["institution_name"]
                if canonical.get("school_id"):
                    record["school_id"] = canonical["school_id"]
                source_record = {"inventory": inventory_name,
                                 "classification_index": classified_name,
                                 "source_urls": record.get("source_urls") or [record.get("source_url", "")],
                                 "catalog_year": record.get("catalog_year"),
                                 "offering_status": record.get("offering_status"),
                                 "materials_research_status": record.get("materials_research_status"),
                                 "source_fingerprint": record.get("source_fingerprint"),
                                 "classification_fingerprint": record.get("classification_fingerprint"),
                                 "evidence_records": course.get("source_records", [])}
                key = record["course_key"]
                prior = by_key.get(key)
                if prior is None:
                    record["source_records"] = [source_record]
                    by_key[key] = record
                    continue

                prior["source_records"].append(source_record)
                if record.get("classification_status") == "needs-review":
                    prior["classification_status"] = "needs-review"
                    prior["review_reasons"] = sorted(set(prior.get("review_reasons", []) +
                                                          record.get("review_reasons", []) +
                                                          ["source-classification-needs-review"]))
                for field in ("source_urls", "catalog_term_labels", "term_offerings"):
                    incoming = record.get(field)
                    if not incoming:
                        continue
                    existing = prior.setdefault(field, [])
                    values = incoming if isinstance(incoming, list) else [incoming]
                    seen = {json.dumps(value, sort_keys=True, ensure_ascii=False) for value in existing}
                    for value in values:
                        marker = json.dumps(value, sort_keys=True, ensure_ascii=False)
                        if marker not in seen:
                            existing.append(value)
                            seen.add(marker)
                for field in ("departments",):
                    incoming = record.get(field)
                    if incoming:
                        existing = prior.setdefault(field, [])
                        seen = {json.dumps(value, sort_keys=True, ensure_ascii=False) for value in existing}
                        for value in incoming:
                            marker = json.dumps(value, sort_keys=True, ensure_ascii=False)
                            if marker not in seen:
                                existing.append(value)
                                seen.add(marker)
                if record.get("description") and record.get("description") != prior.get("description"):
                    variants = prior.setdefault("description_variants", [])
                    variants.append({"inventory": inventory_name, "description": record["description"],
                                     "source_urls": record.get("source_urls", [])})
                if (record.get("primary_academic_area") != prior.get("primary_academic_area") or
                        {area["id"] for area in record.get("academic_areas", [])} !=
                        {area["id"] for area in prior.get("academic_areas", [])} or
                        {tag["id"] for tag in record.get("expertise_tags", [])} !=
                        {tag["id"] for tag in prior.get("expertise_tags", [])}):
                    prior.setdefault("classification_variants", []).append({
                        "inventory": inventory_name,
                        "primary_academic_area": record.get("primary_academic_area"),
                        "academic_areas": record.get("academic_areas", []),
                        "expertise_tags": record.get("expertise_tags", []),
                        "classification_status": record.get("classification_status"),
                        "review_reasons": record.get("review_reasons", []),
                        "classification_evidence": record.get("classification_evidence", {}),
                    })
                prior["materials_research_statuses"] = sorted(set(
                    prior.get("materials_research_statuses", []) +
                    ([str(prior["materials_research_status"])] if prior.get("materials_research_status") else []) +
                    ([str(record["materials_research_status"])] if record.get("materials_research_status") else [])
                ))
    rows = list(by_key.values())
    for row in rows:
        if row.get("classification_variants"):
            row["classification_status"] = "needs-review"
            row["review_reasons"] = sorted(set(
                row.get("review_reasons", []) + ["conflicting-source-classifications"]))
        if row.get("source_fingerprint"):
            row.update(bind_merged_fingerprints(row))
    model_overlay = load_overlay(root)
    if model_overlay is not None:
        taxonomy = json.loads((root / "expertise-taxonomy-v1.json").read_text(encoding="utf-8"))
        rows = [apply_record(row, model_overlay.get(row["course_key"]), taxonomy) for row in rows]
        supplements = load_discovery_supplements(root, taxonomy)
        rows = [apply_discovery_supplements(row, model_overlay.get(row["course_key"]), taxonomy, supplements) for row in rows]
    if reviews is not None:
        try:
            from .classification_review import apply_review
        except ImportError:
            from classification_review import apply_review
        taxonomy = json.loads((root / "expertise-taxonomy-v1.json").read_text(encoding="utf-8"))
        by_review_key = {}
        if not isinstance(reviews, list) or any(not isinstance(review, dict) for review in reviews):
            raise ValueError("Classification reviews must be a list of objects")
        for review in reviews:
            key = review["course_key"]
            if key in by_review_key or key not in by_key:
                raise ValueError(f"Duplicate or unknown review course key: {key}")
            by_review_key[key] = review
        rows = [apply_review(row, by_review_key[row["course_key"]], taxonomy)
                if row["course_key"] in by_review_key else row for row in rows]
    materials_path = root / "course-materials-2026-27" / "course-materials.json.xz"
    if include_materials and materials_path.exists():
        manifest_path = materials_path.with_name("manifest.json")
        material_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        file_entry = next(item for item in material_manifest["files"]
                          if item["file"] == materials_path.name)
        compressed = materials_path.read_bytes()
        if hashlib.sha256(compressed).hexdigest() != file_entry["compressed_sha256"]:
            raise ValueError(f"SHA-256 mismatch: {materials_path}")
        material_payload = json.loads(lzma.decompress(compressed))
        if len(material_payload.get("entries", [])) != file_entry["records"]:
            raise ValueError(f"Record-count mismatch: {materials_path}")
        material_by_key = {item["course_key"]: item for item in material_payload["entries"]}
        for row in rows:
            evidence = material_by_key.get(row["course_key"])
            if evidence:
                row["course_materials"] = {
                    "status": evidence["status"],
                    "evidence_statuses": evidence.get("evidence_statuses", []),
                    "materials": evidence.get("materials", []),
                    "sources": evidence.get("sources", []),
                    "gaps": evidence.get("gaps", []),
                    "research_records": evidence.get("source_evidence_rows", []),
                    "knowledge_processed": evidence.get("knowledge_processed", False),
                }
    return rows


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(item) for item in value.values()]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def matches(course: dict[str, Any], *, institution: str = "", area: str = "",
            expertise: str = "", term: str = "", query: str = "",
            material_status: str = "", resource: str = "") -> bool:
    if institution:
        actual = resolve_institution(course.get("institution", ""))
        requested = resolve_institution(institution)
        same = actual["institution_id"] == requested["institution_id"]
        if requested.get("school_id"):
            if not same or actual.get("school_id") != requested["school_id"]:
                return False
        if requested["in_scope"] and not same:
            return False
        if not requested["in_scope"] and institution.casefold() not in str(course.get("institution", "")).casefold():
            return False
    if area:
        supported = "discovery_academic_areas" in course
        areas = course.get("discovery_academic_areas", []) if supported else course.get("academic_areas", [])
        values = [] if supported else [course.get("primary_academic_area", "")]
        values.extend(item.get("label", "") for item in areas if isinstance(item, dict))
        values.extend(item.get("id", "") for item in areas if isinstance(item, dict))
        if not any(area.casefold() in value.casefold() for value in values):
            return False
    if expertise:
        tags = course.get("expertise_tags", [])
        values = [str(tag.get(key, "")) for tag in tags if isinstance(tag, dict)
                  for key in ("id", "label")]
        if not any(expertise.casefold() in value.casefold() for value in values):
            return False
    if term:
        values = _strings(course.get("catalog_term_labels"))
        for offering in course.get("term_offerings", []):
            if isinstance(offering, dict):
                values.extend(str(value) for key, value in offering.items()
                              if "term" in key.casefold() or "roster" in key.casefold())
        values.extend(_strings(course.get("term_offering")))
        if not any(term.casefold() in value.casefold() for value in values):
            return False
    if query:
        fields = [course.get("code"), course.get("title"), course.get("catalog_department"),
                  course.get("subject_code"), course.get("description")]
        searchable = " ".join(str(value or "") for value in fields).casefold()
        if query.casefold() not in searchable:
            return False
    course_materials = course.get("course_materials", {})
    if material_status:
        statuses = [course_materials.get("status", "")]
        statuses.extend(course_materials.get("evidence_statuses", []))
        if not any(material_status.casefold() in str(value).casefold() for value in statuses):
            return False
    if resource:
        material_values = []
        for material in course_materials.get("materials", []):
            if isinstance(material, dict):
                material_values.extend(str(material.get(key) or "") for key in (
                    "exact_source_title", "exact_source_title_or_citation",
                    "source_title_or_citation", "citation", "title", "resource_title", "authors",
                    "author", "edition", "isbn"))
            else:
                material_values.append(str(material))
        if not any(resource.casefold() in value.casefold() for value in material_values):
            return False
    return True


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return "" if value is None else value


def export_courses(rows: list[dict[str, Any]], format_name: str, out: Path | None) -> None:
    if out is None:
        handle = sys.stdout
    else:
        out = out.resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        handle = out.open("w", newline="", encoding="utf-8")
    try:
        if format_name == "json":
            json.dump(rows, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        else:
            fields = list(dict.fromkeys(key for row in rows for key in row))
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: csv_value(row.get(key)) for key in fields} for row in rows)
    finally:
        if out is not None:
            handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description='Filter the expanded university course map without ranking or hiding evidence.')
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "expansion")
    parser.add_argument("--institution", default="", help="University alias or case-insensitive name substring; child-school aliases remain school-specific")
    parser.add_argument("--area", default="", help="Academic-area ID or label substring")
    parser.add_argument("--expertise", default="", help="Cross-cutting expertise ID or label substring")
    parser.add_argument("--term", default="", help="Catalog term label or roster-term substring")
    parser.add_argument("--query", default="", help="Code, title, department, or description substring")
    parser.add_argument("--materials-status", default="", help="Per-course materials evidence status substring")
    parser.add_argument("--resource", default="", help="Book/material title, citation, author, edition, or ISBN substring")
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    parser.add_argument("--out", type=Path, help="Output path; otherwise write to standard output")
    parser.add_argument("--allow-partial", action="store_true", help="Explicitly allow missing dataset pairs; output is not a complete saved map")
    parser.add_argument("--reviews", type=Path, help="JSON array of source-bound classification reviews")
    args = parser.parse_args()
    try:
        if args.allow_partial:
            print("WARNING: partial map loading is enabled; coverage may be incomplete", file=sys.stderr)
        reviews = json.loads(args.reviews.read_text(encoding="utf-8")) if args.reviews else None
        rows = [row for row in load_course_map(args.root, allow_partial=args.allow_partial, reviews=reviews)
                if matches(row, institution=args.institution, area=args.area,
                           expertise=args.expertise, term=args.term, query=args.query,
                           material_status=args.materials_status, resource=args.resource)]
        export_courses(rows, args.format, args.out)
    except (OSError, ValueError, KeyError, TypeError, lzma.LZMAError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
