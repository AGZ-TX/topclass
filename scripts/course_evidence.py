from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .catalog_identity import resolve_institution
    from .materials_ledger import is_book_material
except ImportError:
    from catalog_identity import resolve_institution
    from materials_ledger import is_book_material


FORMAT = "topclass-verified-course-evidence-v1"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data/course-evidence/verified-courses.json"
ROLES = {"required", "recommended", "supplementary", "optional", "not-stated"}


def checked_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value


def checked_url(value, field):
    checked_text(value, field)
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or any(character.isspace() for character in value):
        raise ValueError(f"{field} must be an HTTPS source URL")
    return value


def checked_list(value, field, *, nonempty=False):
    if not isinstance(value, list) or nonempty and not value:
        raise ValueError(f"{field} must be a {'nonempty ' if nonempty else ''}list")
    return value


def checked_record(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def checked_source(value, field):
    checked_record(value, field)
    checked_url(value.get("source_url"), f"{field}.source_url")
    checked_text(value.get("locator"), f"{field}.locator")
    return value


def validate_course_evidence(payload):
    checked_record(payload, "payload")
    if payload.get("format") != FORMAT:
        raise ValueError("Unsupported course evidence format")
    date.fromisoformat(checked_text(payload.get("checked_on"), "checked_on"))
    rows = []
    passages = {}
    evidence_ids = set()
    for original in checked_list(payload.get("courses"), "courses"):
        row = copy.deepcopy(checked_record(original, "course"))
        for field in ("course_key", "evidence_id", "institution", "code", "title", "source_year", "source_term"):
            checked_text(row.get(field), field)
        if not resolve_institution(row["institution"])["in_scope"]:
            raise ValueError("Course evidence institution is outside the 12-university scope")
        if row["course_key"] in passages or row["evidence_id"] in evidence_ids:
            raise ValueError("Duplicate course key or evidence ID")
        evidence_ids.add(row["evidence_id"])
        checked_url(row.get("source_url"), "course.source_url")
        if row.get("source_verification_status") != "reviewed-primary-source":
            raise ValueError("Course evidence must be reviewed-primary-source")
        if row.get("current_adoption_verified") is not False or row.get("knowledge_processed") is not False:
            raise ValueError("Course supplement cannot claim current adoption or processed book knowledge")
        for record in checked_list(row.get("source_records"), "source_records", nonempty=True):
            checked_source(record, "source_record")
            for field in ("code", "title", "source_year", "source_term"):
                checked_text(record.get(field), f"source_record.{field}")
        for material in checked_list(row.get("materials"), "materials", nonempty=True):
            checked_source(material, "material")
            if material.get("material_type") != "book":
                raise ValueError("Course supplement materials must have material_type book")
            for field in ("title", "citation", "role_evidence"):
                checked_text(material.get(field), f"material.{field}")
            if "exact_source_title" in material and material["exact_source_title"] != material["title"]:
                raise ValueError("material.exact_source_title must match its named title")
            material["exact_source_title"] = material["title"]
            if not is_book_material(material):
                raise ValueError("Course supplement requires named books, not absence statements")
            if material.get("assignment_role") not in ROLES:
                raise ValueError("Unknown material assignment_role")
            for field in ("authors", "edition", "isbn"):
                if field in material and material[field] is not None and not isinstance(material[field], str):
                    raise ValueError(f"material.{field} must be text or null")
            material.setdefault("source_year", row["source_year"])
            material.setdefault("source_verification_status", row["source_verification_status"])
        descriptions = checked_list(row.get("description_passages"), "description_passages", nonempty=True)
        for passage in descriptions:
            checked_source(passage, "description_passage")
            checked_text(passage.get("quote"), "description_passage.quote")
            passage.setdefault("source_year", row["source_year"])
            passage.setdefault("source_term", row["source_term"])
        passages[row["course_key"]] = copy.deepcopy(descriptions)
        row["status"] = "found"
        row["sources"] = [{"source_url": row["source_url"], "source_year": row["source_year"],
                           "source_term": row["source_term"], "checked_on": payload["checked_on"]}]
        rows.append(row)
    return {"reading_rows": rows, "description_passages": passages}


def load_course_evidence(path=None):
    source = Path(path) if path is not None else DEFAULT_PATH
    if not source.exists():
        return {"reading_rows": [], "description_passages": {}}
    return validate_course_evidence(json.loads(source.read_text(encoding="utf-8")))
