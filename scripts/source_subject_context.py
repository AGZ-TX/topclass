import hashlib
import json
from datetime import date
from pathlib import Path
import re

try:
    from .jev_course_classification import course_state, identity_warning, state_fingerprint
except ImportError:
    from jev_course_classification import course_state, identity_warning, state_fingerprint


FORMAT = "topclass-source-subject-context-v1"
FOLDER = "source-subject-context-2026-10-01"
PHRASES = {
    "environment-earth-climate": ["Earth & Planetary Sciences", "Earth's ecosystems", "Geodesy", "CLIMATE CRISIS"],
    "natural-physical-sciences": ["fungal diversity"],
    "psychology-cognitive-science": ["Psychological Development"],
    "health-medicine": ["Mental Illness"],
    "humanities-history-languages": ["nuclear weapons history", "Western culture from Biblical times", "Middle Eastern Humanities",
                                   "SEMANTICS IN GENERATIVE GRAMMAR",
                                   "INTRODUCTION TO FRENCH", "Francophone Language", "GRAMMAIRE PROGRESSIVE DU FRANÇAIS",
                                   "BLACK HISTORY", "HOW DEAD LANGUAGES WORK", "A CENTURY OF FICTION", "A GRAPHIC MYSTERY",
                                   "THE SHORT STORY AS CASE HISTORY", "American & Russian Sci-Fi",
                                   "SLAVE REVOLUTION IN THE CARIBBEAN, 1789-1804", "HISTORY OF THE HAITIAN REVOLUTION",
                                   "THE CORRESPONDENT: A NOVEL", "ARABIAN NIGHTS AND DAYS A NOVEL",
                                   "SLAVERY AND THE JEWS OF MEDIEVAL EGYPT"],
    "law-policy-governance": ["nuclear weapons history and policy", "questions of ethics, policy, and law",
                              "political ideas and institutions", "relations with Congress and bureaucracy",
                              "international relations", "Mergers and Acquisitions; Law",
                              "FIRST AMENDMENT JURISPRUDENCE", "AMERICAN CONSTITUTIONAL LAW", "Authoritarianism"],
    "military-security": ["nuclear weapons"],
    "computing-data-information": ["artificial intelligence"],
    "arts-design-media": ["films from the Middle East", "Screenwriting", "Narrative Filmmaking", "Bach’s music",
                          "ART MUSEUM HANDBOOK OF THE COLLECTIONS", "A GRAPHIC MYSTERY", "DOCUMENTARY FILMMAKING"],
    "social-sciences": ["Culture, Social Norms", "political ideas and institutions", "domestic politics", "international relations",
                        "FOOD JUSTICE", "FEMINIST READER", "HOUSEHOLD EMPLOYERS EXPLOIT DOMESTIC WORKERS",
                        "DISCOURSE ON COLONIALISM", "INDIGENOUS LAND RELATIONS", "SOCIAL SCIENCE EXPERIMENTS", "Authoritarianism"],
    "philosophy-religion-ethics": ["questions of ethics", "trends in theology"],
    "business-economics-management": ["Entrepreneurial Leadership", "Venture Capital", "Mergers and Acquisitions", "DELIVERING CUSTOMER SERVICE"],
    "mathematics-statistics": ["Multivariable Analysis", "Applied Algebra", "Algebraic Geometry",
                              "Mathematical Competition", "Mathematical Introduction to Logic"],
}
POLICY = {"algorithm": "approved-explicit-source-phrase-context-v1", "phrases": PHRASES,
          "anchor_schema_version": "original-source-kind-course-key-source-urls-v2",
          "reading_title_core_algorithm": "literal-original-substring-before-citation-author-delimiter-v1",
          "source_fields": ["title", "course_title", "description", "catalog_department", "academic_subject", "reading_title"],
          "scope": "Candidate retrieval fields only; no primary, identity verification, current adoption, outcomes, learned knowledge or task fit."}
CONTEXT_VERSION = "source-subject-context-v1-" + state_fingerprint(POLICY)[:12]


def source_context(course):
    source = course_state(course)
    for alias, canonical in (("course_code", "code"), ("course_title", "title"), ("university", "institution")):
        if alias in source and source.get(alias) == source.get(canonical):
            source.pop(alias)
    source["course_key"] = course.get("course_key")
    urls = course.get("source_urls", [])
    urls = urls if isinstance(urls, list) else []
    source["source_urls"] = sorted({url for url in [course.get("source_url"), *urls] if isinstance(url, str) and url})
    for field in ("source_year", "catalog_year", "catalog_version", "course_version", "offering_status"):
        if course.get(field):
            source[field] = course[field]
    records = []
    for row in course.get("source_records", []):
        if not isinstance(row, dict):
            raise ValueError("Source context lineage requires records")
        records.append({key: value for key, value in row.items() if key not in ("classification_index", "classification_fingerprint")})
    source["source_records"] = sorted(records, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
    return source


def _bindings(course, readings, taxonomy):
    key = course.get("course_key")
    if not isinstance(key, str) or not key or not isinstance(readings, list):
        raise ValueError("Source context requires a course key and original readings")
    if any(not isinstance(row, dict) or row.get("course_key") != key for row in readings):
        raise ValueError("Source context readings must retain the original course identity")
    return {"course_key": key, "context_version": CONTEXT_VERSION,
            "source_fingerprint": state_fingerprint(source_context(course)),
            "reading_fingerprint": state_fingerprint(readings), "taxonomy_fingerprint": state_fingerprint(taxonomy)}


def _anchors(course, readings, field, quote, book_id):
    if field not in POLICY["source_fields"] or not isinstance(quote, str) or not quote.strip():
        raise ValueError("Source context requires an eligible field and exact quote")
    if field == "reading_title":
        rows = [(index, row) for index, row in enumerate(readings) if row.get("book_id") == book_id]
        if not isinstance(book_id, str) or not book_id or not rows:
            raise ValueError("Source context reading anchor requires its original book identity")
        matches = [(index, row) for index, row in rows if isinstance(row.get("title"), str)
                   and quote in re.split(r"\s—\s|\s/\s", row["title"], maxsplit=1)[0]]
        if not matches:
            raise ValueError("Source context quote is absent from the original reading title")
        return [{"kind": "reading", "course_key": course["course_key"], "source_urls": row.get("source_urls", []),
                 "field": field, "quote": quote, "book_id": book_id, "reading_index": index,
                 "source_text_fingerprint": state_fingerprint(row["title"]), "reading_lineage": [row]} for index, row in matches]
    if book_id is not None:
        raise ValueError("Course source anchors cannot carry a book identity")
    text = course.get(field)
    if not isinstance(text, str) or quote not in text:
        raise ValueError("Source context quote is absent from the original source field")
    return [{"kind": "course", "course_key": course["course_key"], "source_urls": source_context(course)["source_urls"],
             "field": field, "quote": quote, "source_text_fingerprint": state_fingerprint(text),
             "source_lineage": source_context(course)}]


def _proofs(quote, identifiers, taxonomy):
    labels = {area["id"]: area["label"] for area in taxonomy["academic_areas"]}
    if (not isinstance(quote, str) or not quote.strip() or not isinstance(identifiers, list) or not identifiers
            or any(not isinstance(identifier, str) for identifier in identifiers) or len(set(identifiers)) != len(identifiers)
            or any(identifier not in labels for identifier in identifiers)):
        raise ValueError("Source context areas must be distinct current taxonomy IDs")
    proofs = []
    for identifier in sorted(identifiers):
        found = []
        for phrase in PHRASES.get(identifier, []):
            match = re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", quote, flags=re.IGNORECASE)
            if match:
                found.append({"academic_area_id": identifier, "phrase": phrase, "matched_text": match.group(0),
                              "match_start": match.start(), "match_end": match.end(), "method": POLICY["algorithm"]})
        if not found:
            raise ValueError("Each source context area requires an approved explicit phrase proof")
        proofs.extend(found)
    return proofs


def context_record(course, readings, taxonomy, *, field, quote, academic_area_ids, reviewer, reviewed_on, rationale,
                   group_id=None, book_id=None):
    if (not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(rationale, str) or not rationale.strip()
            or not isinstance(reviewed_on, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", reviewed_on)):
        raise ValueError("Source context candidates require explicit dated reviewer and rationale")
    date.fromisoformat(reviewed_on)
    bindings = _bindings(course, readings, taxonomy)
    anchors = _anchors(course, readings, field, quote, book_id)
    proofs = _proofs(quote, academic_area_ids, taxonomy)
    basis = "source-explicit-reading-context-candidate" if field == "reading_title" else "source-named-department-context-candidate" if field in ("catalog_department", "academic_subject") else "source-explicit-course-text-candidate"
    result = bindings | {"source_field": field, "source_quote": quote, "source_text_fingerprint": anchors[0]["source_text_fingerprint"],
        "academic_area_ids": sorted(academic_area_ids), "phrase_proofs": proofs,
        "anchors": anchors, "basis": basis, "group_id": group_id, "book_id": book_id,
        "reviewer": reviewer, "reviewed_on": reviewed_on, "rationale": rationale,
        "review_status": "agent-reviewed-retrieval-candidate", "human_reviewed": False,
        "identity_warning": identity_warning(course), "current_adoption_verified": False, "task_fit_verified": False}
    result["record_fingerprint"] = state_fingerprint(result)
    return result


def source_subject_facets(course, readings, taxonomy, records):
    if not isinstance(records, list):
        raise ValueError("Source context evidence must contain records")
    labels = {area["id"]: area["label"] for area in taxonomy["academic_areas"]}
    facets = {}
    for record in records:
        if (not isinstance(record, dict) or record.get("record_fingerprint") != state_fingerprint({key: value for key, value in record.items() if key != "record_fingerprint"})):
            raise ValueError("Source context record integrity mismatch")
        rebuilt = context_record(course, readings, taxonomy, field=record["source_field"], quote=record["source_quote"],
            academic_area_ids=record["academic_area_ids"], reviewer=record["reviewer"], reviewed_on=record["reviewed_on"],
            rationale=record["rationale"], group_id=record.get("group_id"), book_id=record.get("book_id"))
        if rebuilt != record:
            raise ValueError("Source context candidate no longer matches the original source, reading, taxonomy or policy")
        for identifier in record["academic_area_ids"]:
            facet = facets.setdefault(identifier, {"id": identifier, "label": labels[identifier], "basis": record["basis"],
                "context_bases": [], "anchor_candidates": [], "record_fingerprints": [],
                **_bindings(course, readings, taxonomy), "identity_warning": identity_warning(course),
                "human_reviewed": False, "current_adoption_verified": False, "task_fit_verified": False})
            facet["context_bases"].append(record["basis"])
            facet["record_fingerprints"].append(record["record_fingerprint"])
            proofs = [proof for proof in record["phrase_proofs"] if proof["academic_area_id"] == identifier]
            facet["anchor_candidates"].extend(anchor | {"phrase_proofs": proofs, "reviewer": record["reviewer"],
                "reviewed_on": record["reviewed_on"], "record_fingerprint": record["record_fingerprint"]} for anchor in record["anchors"])
    for facet in facets.values():
        facet["context_bases"] = sorted(set(facet["context_bases"]))
        facet["record_fingerprints"] = sorted(set(facet["record_fingerprints"]))
        facet["anchor_candidates"].sort(key=lambda anchor: (anchor["field"], anchor.get("reading_index", -1), anchor["quote"]))
        if len(facet["context_bases"]) > 1:
            facet["basis"] = "source-explicit-mixed-context-candidate"
    return [facets[key] for key in sorted(facets)]


def context_index(payload, taxonomy):
    if (not isinstance(payload, dict) or payload.get("format") != FORMAT or payload.get("context_version") != CONTEXT_VERSION
            or payload.get("taxonomy_fingerprint") != state_fingerprint(taxonomy) or not isinstance(payload.get("records"), list)):
        raise ValueError("Source context registration version, taxonomy or records are invalid")
    indexed = {}
    fingerprints = set()
    for record in payload["records"]:
        key = record.get("course_key") if isinstance(record, dict) else None
        if (not isinstance(key, str) or not key or record.get("context_version") != CONTEXT_VERSION
                or record.get("taxonomy_fingerprint") != state_fingerprint(taxonomy)
                or record.get("record_fingerprint") != state_fingerprint({field: value for field, value in record.items() if field != "record_fingerprint"})
                or record["record_fingerprint"] in fingerprints):
            raise ValueError("Source context registration contains invalid, stale or duplicate evidence")
        fingerprints.add(record["record_fingerprint"])
        if record.get("source_field") not in POLICY["source_fields"] or record.get("phrase_proofs") != _proofs(record.get("source_quote"), record.get("academic_area_ids"), taxonomy):
            raise ValueError("Source context registration contains invalid source phrase proofs")
        indexed.setdefault(key, []).append(record)
    return indexed


def load_source_subject_context(root, taxonomy):
    folder = Path(root) / FOLDER
    if not folder.exists():
        return {}
    paths = [folder, folder / "manifest.json", folder / "evidence.json"]
    if any(path.is_symlink() for path in [*paths, *folder.absolute().parents]):
        raise ValueError("Source context registration cannot use symlinks")
    manifest = json.loads(paths[1].read_text(encoding="utf-8"))
    raw = paths[2].read_bytes()
    if (manifest.get("format") != FORMAT + "-manifest" or manifest.get("context_version") != CONTEXT_VERSION
            or manifest.get("evidence_sha256") != hashlib.sha256(raw).hexdigest()
            or manifest.get("evidence_bytes") != len(raw)):
        raise ValueError("Source context manifest integrity mismatch")
    payload = json.loads(raw)
    if manifest.get("records") != len(payload.get("records", [])):
        raise ValueError("Source context manifest record count mismatch")
    report = folder / "review.json"
    if report.is_symlink() or manifest.get("review_sha256") != hashlib.sha256(report.read_bytes()).hexdigest():
        raise ValueError("Source context review report integrity mismatch")
    return context_index(payload, taxonomy)
