#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import lzma
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from .classify_courses import TAXONOMY_PATH, fingerprint, normalize, validate_taxonomy
except ImportError:
    from classify_courses import TAXONOMY_PATH, fingerprint, normalize, validate_taxonomy


def label_ids(record: dict[str, Any], field: str) -> set[str]:
    return {str(item["id"]) for item in record.get(field, [])}


def source_text(record: dict[str, Any]) -> list[str]:
    evidence = record.get("classification_evidence", {})
    values = [record.get(field, "") for field in
              ("code", "course_code", "title", "course_title", "description", "academic_subject",
               "catalog_department", "school", "college", "subject_code")]
    values.extend(evidence.get("official_department_labels", []))
    values.extend(evidence.get("official_school_labels", []))
    values.extend(evidence.get("source_text_fields", {}).values())
    values.extend(item.get("description", "") for item in record.get("description_variants", []))
    return [normalize(value) for value in values if isinstance(value, str) and value]


def apply_review(record: dict[str, Any], review: dict[str, Any], taxonomy: dict[str, Any]) -> dict[str, Any]:
    if record.get("classification_status") == "reviewed":
        raise ValueError("Review requires the original candidate classification")
    if record.get("taxonomy_version") and record["taxonomy_version"] != taxonomy["version"]:
        raise ValueError("Review taxonomy version differs from the candidate classification")
    for field in ("course_key", "source_fingerprint", "classification_fingerprint"):
        if not record.get(field) or review.get(field) != record[field]:
            raise ValueError(f"Missing or stale review {field}: {record.get('course_key')}")
    for field in ("reviewer", "rationale"):
        if not isinstance(review.get(field), str) or not review[field].strip():
            raise ValueError(f"Review requires {field}")
    sources = set(record.get("source_urls") or [])
    if record.get("source_url"):
        sources.add(record["source_url"])
    for source in record.get("source_records", []):
        sources.update(source.get("source_urls") or [])
    evidence = review.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("Review requires quoted source evidence")
    text = source_text(record)
    for item in evidence:
        if not isinstance(item, dict) or item.get("source_url") not in sources:
            raise ValueError("Review evidence source must occur in the course source record")
        quote = item.get("quote")
        if not isinstance(quote, str) or not normalize(quote) or not any(normalize(quote) in value for value in text):
            raise ValueError("Review quote must occur in saved course fields")
    areas = {item["id"]: item for item in taxonomy["academic_areas"]}
    areas["other-academic-subject"] = {"id": "other-academic-subject", "label": "Other academic subject"}
    domains = {item["id"]: item for item in taxonomy["expertise_domains"]}
    for field, allowed in (("academic_area_ids", areas), ("expertise_tag_ids", domains)):
        ids = review.get(field)
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            raise ValueError(f"Review requires explicit {field}")
        if len(ids) != len(set(ids)) or not set(ids).issubset(allowed):
            raise ValueError(f"Review has duplicate or unknown {field}")
    if not review["academic_area_ids"] or review.get("primary_academic_area") not in review["academic_area_ids"]:
        raise ValueError("Review primary area must be an explicitly selected academic area")
    result = copy.deepcopy(record)
    original = {field: copy.deepcopy(record.get(field)) for field in
                ("academic_areas", "expertise_tags", "primary_academic_area", "primary_candidates",
                 "primary_resolution", "classification_status", "review_reasons")}
    result["academic_areas"] = [{"id": value, "label": areas[value]["label"],
                                 "basis": "reviewed-source-evidence", "matched_evidence": copy.deepcopy(evidence)}
                                for value in sorted(review["academic_area_ids"])]
    result["expertise_tags"] = [{"id": value, "label": domains[value]["label"],
                                 "academic_area_ids": domains[value]["academic_area_ids"],
                                 "basis": "reviewed-source-evidence", "matched_evidence": copy.deepcopy(evidence)}
                                for value in sorted(review["expertise_tag_ids"])]
    result["primary_academic_area"] = review["primary_academic_area"]
    result["primary_resolution"] = "reviewed"
    result["primary_candidates"] = [review["primary_academic_area"]]
    result["classification_status"] = "reviewed"
    result["review_reasons"] = []
    result["classification_review"] = {**copy.deepcopy(review), "candidate_classification": original}
    result["review_fingerprint"] = fingerprint(review)
    return result


def unique_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError("Input must be a list of course records")
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Each input record must be an object")
        key = record.get("course_key")
        if not isinstance(key, str) or not key or key in result:
            raise ValueError("Input records require distinct nonempty course_key values")
        result[key] = record
    return result


def review_template(record: dict[str, Any]) -> dict[str, Any]:
    return {"course_key": record["course_key"], "source_fingerprint": record.get("source_fingerprint", ""),
            "classification_fingerprint": record.get("classification_fingerprint", ""),
            "reviewer": "", "rationale": "", "evidence": [],
            "academic_area_ids": [], "expertise_tag_ids": [], "primary_academic_area": ""}


def stratified_sample(records: list[dict[str, Any]], *, limit: int = 200, seed: str = "topclass-v2") -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("Sample limit must be positive")
    by_key = unique_records(records)
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in by_key.values():
        if record.get("classification_status") == "reviewed":
            continue
        buckets[(str(record.get("classification_status", "")), str(record.get("institution", "")),
                 str(record.get("primary_academic_area", "other-academic-subject")))].append(record)
    for bucket in buckets.values():
        bucket.sort(key=lambda record: fingerprint([seed, record["course_key"]]))
    grouped: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for stratum in sorted(buckets):
        grouped[stratum[0]].append(stratum)
    strata = []
    while any(grouped.values()):
        for status in sorted(grouped):
            if grouped[status]:
                strata.append(grouped[status].pop(0))
    selected = []
    while len(selected) < limit and any(buckets.values()):
        for stratum in strata:
            if buckets[stratum] and len(selected) < limit:
                record = copy.deepcopy(buckets[stratum].pop(0))
                record["review_template"] = review_template(record)
                selected.append(record)
    return selected


def metrics(counts: tuple[int, int, int]) -> dict[str, Any]:
    true_positive, false_positive, false_negative = counts
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else None
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else None
    f1 = 2 * true_positive / (2 * true_positive + false_positive + false_negative) if any(counts) else None
    return {"true_positive": true_positive, "false_positive": false_positive,
            "false_negative": false_negative, "precision": precision, "recall": recall, "f1": f1,
            "support": true_positive + false_negative}


def evaluation_counts(pairs: list[tuple[dict[str, Any], dict[str, Any]]], field: str) -> dict[str, Any]:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for candidate, reviewed in pairs:
        predicted = label_ids(candidate, field)
        expected = label_ids(reviewed, field)
        for label in predicted | expected:
            if label in predicted and label in expected:
                counts[label][0] += 1
            elif label in predicted:
                counts[label][1] += 1
            else:
                counts[label][2] += 1
    labels = {label: metrics(tuple(values)) for label, values in sorted(counts.items())}
    totals = tuple(sum(values[index] for values in counts.values()) for index in range(3))
    macro = {}
    for measure in ("precision", "recall", "f1"):
        values = [item[measure] for item in labels.values() if item[measure] is not None]
        macro[measure] = sum(values) / len(values) if values else None
        macro[f"{measure}_labels"] = len(values)
    return {"by_label": labels, "micro": metrics(totals), "macro": macro,
            "evaluated_labels": len(labels), "reviewed_courses": len(pairs)}


def evaluate(records: list[dict[str, Any]], reviews: list[dict[str, Any]], taxonomy: dict[str, Any]) -> dict[str, Any]:
    by_key = unique_records(records)
    review_index = unique_records(reviews)
    unknown = set(review_index) - set(by_key)
    if unknown:
        raise ValueError(f"Review courses absent from evaluation input: {', '.join(sorted(unknown))}")
    pairs = [(by_key[key], apply_review(by_key[key], review_index[key], taxonomy)) for key in sorted(review_index)]
    institutions: dict[str, list[Any]] = defaultdict(list)
    for pair in pairs:
        institutions[str(pair[0].get("institution", ""))].append(pair)
    return {"reviewed_courses": len(pairs), "unreviewed_courses": len(records) - len(pairs),
            "representative_accuracy_established": False,
            "evaluation_scope": "Only explicit fingerprint-matched source-evidenced reviews; sample representativeness is not established.",
            "taxonomy_version": taxonomy["version"],
            "academic_areas": evaluation_counts(pairs, "academic_areas"),
            "expertise_tags": evaluation_counts(pairs, "expertise_tags"),
            "institutions": {name: {"reviewed_courses": len(group),
                                     "academic_areas": evaluation_counts(group, "academic_areas"),
                                     "expertise_tags": evaluation_counts(group, "expertise_tags")}
                             for name, group in sorted(institutions.items())}}


def read_records(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    payload = json.loads(lzma.decompress(raw) if path.suffix == ".xz" else raw)
    if isinstance(payload, list):
        if any(not isinstance(item, dict) for item in payload):
            raise ValueError(f"Each record must be an object in {path}")
        return payload
    if isinstance(payload, dict):
        for field in ("classifications", "courses", "records", "reviews", "sample"):
            if isinstance(payload.get(field), list):
                if any(not isinstance(item, dict) for item in payload[field]):
                    raise ValueError(f"Each record must be an object in {path}")
                return payload[field]
    raise ValueError(f"Expected a record list in {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample candidate classifications and measure explicit reviewed labels.")
    parser.add_argument("--taxonomy", type=Path, default=TAXONOMY_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sample = subparsers.add_parser("sample")
    sample_source = sample.add_mutually_exclusive_group(required=True)
    sample_source.add_argument("--input", type=Path)
    sample_source.add_argument("--map-root", type=Path, help="Sample the merged saved map without writing a complete intermediate export")
    sample.add_argument("--limit", type=int, default=200)
    sample.add_argument("--seed", default="topclass-v2")
    sample.add_argument("--out", type=Path)
    evaluation = subparsers.add_parser("evaluate")
    evaluation.add_argument("--input", type=Path, required=True)
    evaluation.add_argument("--reviews", type=Path, required=True)
    evaluation.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8"))
        validate_taxonomy(taxonomy)
        if args.command == "sample" and args.map_root:
            from course_map import load_course_map
            records = load_course_map(args.map_root, include_materials=False)
        else:
            records = read_records(args.input)
        if args.command == "sample":
            selected = stratified_sample(records, limit=args.limit, seed=args.seed)
            payload = {"sample": selected, "input_courses": len(records), "sample_courses": len(selected),
                       "seed": args.seed, "reviewed_courses": 0, "representative_accuracy_established": False,
                       "sampling_strata": ["classification_status", "institution", "primary_academic_area"]}
        else:
            payload = evaluate(records, read_records(args.reviews), taxonomy)
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if args.out:
            with args.out.open("x", encoding="utf-8") as stream:
                stream.write(text)
        else:
            print(text, end="")
    except (OSError, ValueError, KeyError, TypeError, lzma.LZMAError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
