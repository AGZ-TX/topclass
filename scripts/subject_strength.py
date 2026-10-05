import copy
import datetime
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from catalog_identity import resolve_institution


DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "subject-strength.json"


def validate_subject_strength(index):
    if not isinstance(index, dict) or index.get("schema_version") != 1:
        raise ValueError("Unsupported subject-strength schema")
    mappings = index.get("subject_mappings")
    if not isinstance(mappings, list) or not isinstance(index.get("records"), list):
        raise ValueError("Subject mappings and records must be lists")
    subjects = set()
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ValueError("Subject mappings must be objects")
        subject = mapping.get("subject_id")
        if not isinstance(subject, str) or not subject or subject in subjects or mapping.get("review_state") != "reviewed":
            raise ValueError("Subject mappings require unique reviewed identities")
        terms = mapping.get("task_phrases")
        if not isinstance(terms, list) or not terms or any(not isinstance(term, str) or not term.strip() for term in terms):
            raise ValueError("Subject mappings require reviewed task phrases")
        subjects.add(subject)
    seen = set()
    for record in index["records"]:
        if not isinstance(record, dict) or record.get("subject_id") not in subjects or record.get("review_state") != "verified":
            raise ValueError("Ranking evidence requires a verified mapped subject")
        for field in ("institution", "provider", "provider_subject", "methodology_version", "attribution"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ValueError("Ranking evidence requires " + field)
        year = record.get("year")
        if type(year) is not int or not 1900 <= year <= 2200:
            raise ValueError("Ranking year must be an integer")
        try:
            datetime.date.fromisoformat(record["retrieved_on"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Ranking retrieval date must be ISO dated") from error
        for field in ("source_url", "ranking_url", "methodology_url"):
            url = urlparse(record.get(field, ""))
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Ranking sources must use public HTTPS URLs")
        band = record.get("rank_band", {})
        if (type(band.get("low")) is not int or type(band.get("high")) is not int or
                not 1 <= band["low"] <= band["high"] or type(band.get("tied")) is not bool):
            raise ValueError("Ranking requires a valid positive rank band")
        key = (resolve_institution(record["institution"])["institution_id"], record["provider"], year, record["provider_subject"])
        if key in seen:
            raise ValueError("Duplicate institution ranking evidence")
        seen.add(key)
    return index


def load_subject_strength(path=None):
    return validate_subject_strength(json.loads(Path(path or DEFAULT_PATH).read_text(encoding="utf-8")))


def mapped_subjects(requirement, index):
    words = " ".join(re.findall(r"[a-z0-9]+", str(requirement).casefold()))
    return [mapping["subject_id"] for mapping in index["subject_mappings"]
            if any(re.search(r"\b" + re.escape(" ".join(re.findall(r"[a-z0-9]+", phrase.casefold()))) + r"\b", words)
                   for phrase in mapping["task_phrases"])]


def strength_evidence(payload, requirement, index=None):
    index = index if index is not None else load_subject_strength()
    subjects = mapped_subjects(requirement, index)
    institution = resolve_institution(str(payload.get("institution") or payload.get("university") or ""))["institution_id"]
    records = [copy.deepcopy(record) for record in index["records"]
               if len(subjects) == 1 and record["subject_id"] == subjects[0]
               and resolve_institution(record["institution"])["institution_id"] == institution]
    return {"requirement": requirement, "status": "verified-context" if records else "unknown",
            "subject_ids": subjects, "evidence": records,
            "scope": "institution subject strength; does not establish course or book quality",
            "reason": "verified institution evidence" if records else
                      "ambiguous subject mapping" if len(subjects) > 1 else
                      "no reviewed subject mapping" if not subjects else "no verified evidence for this institution"}


def candidate_payload(item):
    return (item.get("node") or item.get("course") or {}).get("payload", {})


def evidence_group(record):
    return (record["provider"], record["year"], record["provider_subject"], record["methodology_version"])


def rerank_candidates(items, requirement, fit_key, index=None):
    index = index if index is not None else load_subject_strength()
    result = list(items)
    groups = defaultdict(list)
    for position, item in enumerate(items):
        groups[fit_key(item)].append(position)
    for positions in groups.values():
        if len(positions) < 2 or any(not items[position].get("named_material") for position in positions):
            continue
        evidence = [{evidence_group(record): record for record in
                     strength_evidence(candidate_payload(items[position]), requirement, index)["evidence"]}
                    for position in positions]
        common = set(evidence[0]).intersection(*(set(records) for records in evidence[1:]))
        if not common:
            continue
        provider_order = index.get("comparison_provider_order", [])
        group = min(common, key=lambda value: (provider_order.index(value[0]) if value[0] in provider_order else len(provider_order), -value[1], value))
        bands = [records[group]["rank_band"] for records in evidence]
        if any((a["low"], a["high"]) != (b["low"], b["high"]) and max(a["low"], b["low"]) <= min(a["high"], b["high"])
               for a in bands for b in bands):
            continue
        ordered = sorted(zip(positions, bands), key=lambda value: (value[1]["low"], value[1]["high"]))
        for position, (original, band) in zip(positions, ordered):
            result[position] = items[original]
    return result


def annotate_plan(plan, index=None):
    index = index if index is not None else load_subject_strength()
    result = copy.deepcopy(plan)
    for field in ("courses", "must_have_courses", "supplemental_courses"):
        for item in result.get(field, []):
            requirements = list(dict.fromkeys(match["requirement"] for match in item.get("matches", []) if match.get("requirement")))
            item["school_strength"] = [strength_evidence(candidate_payload(item), requirement, index) for requirement in requirements]
    result["subject_strength_policy"] = {
        "version": index.get("version"), "use": "tie-breaker after task fit and documented books",
        "comparison": "same provider, year, subject and methodology; unknown preserves order",
        "coverage": "partial verified evidence; missing ranks are unknown", "course_quality_verified": False}
    return result
