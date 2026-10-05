import copy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlparse


ROOT = Path(__file__).resolve().parents[1]
FOLDER = "public-subject-evidence-2026-10-01"
SUBJECT_PATTERNS = {
    "engineering-applied-technology": r"\bengineering\b",
    "computing-data-information": r"\bhuman-computer interaction\b|\bsoftware design\b|\bcomputer science\b|\bdata mining\b",
    "psychology-cognitive-science": r"\bpsychology\b|\bcognitive science\b|\bvisual neuroscience\b|\bneural circuit properties\b|\bhuman psychological variation\b",
    "natural-physical-sciences": r"\bvisual neuroscience\b|\bfunctional magnetic resonance imaging\b|\bdiffusion tensor imaging\b|\bquantum mechanics\b|\bastrophysics and cosmology\b|\bbiology and medicine\b",
    "mathematics-statistics": r"\bmathematical logic\b|\bprobability and statistics\b",
    "business-economics-management": r"\bentrepreneurship\b|\beconomics\b",
    "philosophy-religion-ethics": r"\bphilosophical foundations\b|\bphilosophy of socrates\b",
    "law-policy-governance": r"\bforeign policy objectives\b|\blaw, and politics\b",
    "health-medicine": r"\bbiology and medicine\b",
    "social-sciences": r"\banthropology\b",
}
DATE_FIELDS = {"year": ("source_year", "catalog_year", "year"), "term": ("source_term", "term"),
               "snapshot": ("source_snapshot", "snapshot"), "version": ("course_version", "catalog_version", "version")}


def normalized_identity(value):
    return " ".join(str(value).casefold().split())


def identity_dates(course):
    sources = [course, *[row for row in course.get("source_records") or [] if isinstance(row, dict)]]
    return {name: sorted({normalized_identity(source[field]) for source in sources for field in aliases
                         if source.get(field) not in (None, "")}) for name, aliases in DATE_FIELDS.items()}


def identity_matches(course, expected):
    for name, aliases in (("title", ("title", "course_title")), ("code", ("code", "course_code"))):
        values = {normalized_identity(course[field]) for field in aliases if course.get(field) not in (None, "")}
        if values != {normalized_identity(expected[name])}:
            return False
    return identity_dates(course) == expected["dates"]


def public_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return (parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
            and not any(re.search(r"token|password|secret|credential|signature|api.?key", key, re.I)
                        for key, _ in parse_qsl(parsed.query)))


def validate_evidence(rows, taxonomy):
    allowed = {area["id"] for area in taxonomy["academic_areas"]}
    keys = set()
    if not isinstance(rows, list):
        raise ValueError("Public subject evidence requires a record list")
    for row in rows:
        key = row.get("source_course_key") if isinstance(row, dict) else None
        if not isinstance(key, str) or not key or key in keys or not public_url(row.get("source_url")):
            raise ValueError("Public subject evidence requires unique source identities and safe literal URLs")
        keys.add(key)
        if "binding_source_url" in row:
            if not public_url(row["binding_source_url"]) or not re.fullmatch(r"[a-f0-9]{64}", row.get("original_description_sha256", "")) or row.get("source_status") not in {"indexed-public-official-metadata", "public-secondary-course-metadata"}:
                raise ValueError("Corroborating source evidence requires original description and source bindings")
        binding = row.get("source_identity")
        if not isinstance(binding, dict) or any(not isinstance(binding.get(field), str) or not binding[field].strip() for field in ("title", "code")) or not isinstance(binding.get("dates"), dict) or set(binding["dates"]) != set(DATE_FIELDS):
            raise ValueError("Public subject evidence requires frozen original title, code and date bindings")
        for values in binding["dates"].values():
            if not isinstance(values, list) or any(not isinstance(value, str) or not value or value != normalized_identity(value) for value in values) or values != sorted(set(values)):
                raise ValueError("Public subject date bindings must be canonical original values")
        quotes = row.get("quotes")
        subjects = row.get("candidate_subjects")
        if not isinstance(quotes, list) or not quotes or any(not isinstance(quote, dict) or not isinstance(quote.get("quote"), str) or not quote["quote"].strip() or not isinstance(quote.get("locator"), str) or not quote["locator"] for quote in quotes):
            raise ValueError("Public subject evidence requires exact located quotations")
        if not isinstance(subjects, list) or not subjects or len(set(subjects)) != len(subjects):
            raise ValueError("Public subject evidence requires distinct supported fields")
        for subject in subjects:
            pattern = SUBJECT_PATTERNS.get(subject)
            if subject not in allowed or not pattern or not any(re.search(pattern, quote["quote"], re.I) for quote in quotes):
                raise ValueError("Public subject field lacks curated literal subject support")
        if any(row.get(field) is not False for field in ("current_adoption_verified", "task_fit_verified", "human_reviewed")):
            raise ValueError("Public subject evidence must remain unreviewed discovery metadata")
    return rows


def load_evidence(taxonomy, root=ROOT):
    folder = Path(root) / "data" / "expansion" / FOLDER
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    raw = (folder / "evidence.json").read_bytes()
    if manifest.get("format") != "topclass-public-subject-evidence-v1" or manifest.get("evidence_sha256") != hashlib.sha256(raw).hexdigest():
        raise ValueError("Public subject evidence manifest or snapshot hash changed")
    rows = json.loads(raw)["records"]
    if manifest.get("records") != len(rows):
        raise ValueError("Public subject evidence record count changed")
    return validate_evidence(rows, taxonomy)


def apply_subject_evidence(course, taxonomy, evidence_rows=None, source_urls=None):
    rows = load_evidence(taxonomy) if evidence_rows is None else validate_evidence(evidence_rows, taxonomy)
    result = copy.deepcopy(course)
    original_urls = set()
    sources = [course, *[row for row in course.get("source_records") or [] if isinstance(row, dict)]]
    for source in sources:
        if isinstance(source.get("source_url"), str):
            original_urls.add(source["source_url"])
        urls = source.get("source_urls") or []
        original_urls.update(url for url in ([urls] if isinstance(urls, str) else urls) if isinstance(url, str))
    if source_urls is not None:
        original_urls.update(url for url in ([source_urls] if isinstance(source_urls, str) else source_urls) if isinstance(url, str))
    key = course.get("course_key") or course.get("source_course_key")
    description = course.get("description", course.get("classification_evidence", {}).get("source_text_fields", {}).get("description", ""))
    description_hash = hashlib.sha256(str(description).encode()).hexdigest()
    match = next((row for row in rows if row["source_course_key"] == key and row.get("binding_source_url", row["source_url"]) in original_urls
                  and identity_matches(course, row["source_identity"])
                  and ("original_description_sha256" not in row or row["original_description_sha256"] == description_hash)), None)
    if match is None:
        return result
    labels = {area["id"]: area["label"] for area in taxonomy["academic_areas"]}
    facets = {facet["id"]: facet for facet in result.get("discovery_academic_areas", [])}
    additions = []
    for subject in match["candidate_subjects"]:
        anchors = [{"kind": "course", "field": quote.get("field", "subject_description"), "quote": quote["quote"],
                    "locator": quote["locator"], "course_key": key, "source_urls": [match["source_url"]],
                    "source_year": match.get("source_year"), "source_term": match.get("source_term")}
                   for quote in match["quotes"] if re.search(SUBJECT_PATTERNS[subject], quote["quote"], re.I)]
        facet = {"id": subject, "label": labels[subject], "basis": "public-source-subject-evidence-candidate",
                 "anchor_candidates": anchors, "human_reviewed": False, "current_adoption_verified": False,
                 "task_fit_verified": False, "source_fingerprint": hashlib.sha256(json.dumps(match, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        if "source_status" in match:
            facet["source_status"] = match["source_status"]
        additions.append(facet)
        facets.setdefault(subject, facet)
    result["discovery_academic_areas"] = [facets[subject] for subject in sorted(facets)]
    result["public_subject_evidence"] = {"source_course_key": key, "source_url": match["source_url"],
        "source_year": match.get("source_year"), "source_term": match.get("source_term"),
        "identity_binding": match["identity_binding"], "facets": additions, "human_reviewed": False,
        "current_adoption_verified": False, "task_fit_verified": False}
    return result
