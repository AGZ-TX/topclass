from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.error
import urllib.parse
from datetime import date
from pathlib import Path

FORMAT = "topclass-teaching-descriptions-v1"
PUBLIC_EVIDENCE = "reviewed-public-teaching-v1"
PRIMARY_DOMAINS = {"princeton.edu", "stanford.edu", "slac.stanford.edu", "columbia.edu", "cmu.edu",
                   "cornell.edu", "dartmouth.edu", "harvard.edu", "mit.edu", "berkeley.edu", "upenn.edu", "yale.edu", "brown.edu"}


def normalized_anchor(value):
    return re.sub(r"\W", "", value).casefold()


def code_aliases(value):
    pieces = [part.strip() for part in value.split("/")]
    prefix = re.match(r"(.*\D)\d+\w*$", pieces[0])
    return {normalized_anchor((prefix[1] if prefix and part.isdigit() else "") + part) for part in pieces}


def verified_renumbering(record, binding, evidence):
    receipt = record.get("code_equivalence")
    if not isinstance(receipt, dict):
        return False
    parts = urllib.parse.urlsplit(receipt.get("source_url", ""))
    if parts.scheme != "https" or not (parts.hostname or "").endswith(".yale.edu") or parts.username or parts.password or parts.port not in {None, 443}:
        return False
    if not (urllib.parse.urlsplit(record["source_url"]).hostname or "").endswith(".yale.edu"):
        return False
    old, new = receipt.get("old_code", ""), receipt.get("new_code", "")
    quote = receipt.get("quote", "")
    if not all(isinstance(value, str) and value.strip() for value in (old, new, quote, receipt.get("locator", ""))):
        return False
    tokens = re.findall(r"[A-Za-z]+\s*\d+", quote)
    return (normalized_anchor(old) == normalized_anchor(evidence["code"])
            and normalized_anchor(new) == normalized_anchor(binding["code"])
            and {normalized_anchor(old), normalized_anchor(new)}.issubset({normalized_anchor(token) for token in tokens})
            and bool(re.fullmatch(r"[a-f0-9]{64}", receipt.get("source_document_sha256", ""))))


def verified_course_referral(record, binding):
    receipt = record.get("source_referral", {})
    if not isinstance(receipt, dict) or record.get("identity_binding") != "saved-reading-source-referral-to-exact-course-syllabus":
        return False
    return (binding.get("course_key") == "topclass-v0.4:C364"
            and binding.get("source_url") == "https://cs169.org/fa26/"
            and binding.get("book_evidence_urls") == receipt.get("source_url") == "https://cs169.org/fa26/textbook/"
            and record.get("source_url") == receipt.get("target_url") == "https://cs169.org/fa26/syllabus/"
            and receipt.get("link_text") == "Syllabus"
            and bool(receipt.get("locator"))
            and bool(re.fullmatch(r"[a-f0-9]{64}", receipt.get("source_document_sha256", ""))))


def validate_public_record(record):
    binding = record.get("identity")
    if not isinstance(binding, dict) or any(not isinstance(binding.get(key), str) or not binding[key].strip() for key in ("course_key", "title", "code")):
        raise ValueError("Public teaching evidence requires original course identity")
    if not any(binding.get(key) for key in ("source_year", "catalog_year", "catalog_term_labels", "version", "course_version", "book_evidence_versions")):
        raise ValueError("Public teaching evidence requires original version binding")
    parts = urllib.parse.urlsplit(record.get("source_url", ""))
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.port not in {None, 443} or parts.username or parts.password or not (any(host == domain or host.endswith("." + domain) for domain in PRIMARY_DOMAINS) or verified_course_referral(record, binding)):
        raise ValueError("Public teaching evidence requires an official university source")
    if record.get("source_verification_status") != "reviewed-primary-source" or not str(record.get("locator", "")).strip():
        raise ValueError("Public teaching evidence requires reviewed source and passage locator")
    if not re.fullmatch(r"[a-f0-9]{64}", record.get("source_document_sha256", "")):
        raise ValueError("Public teaching source document hash missing")
    evidence = record.get("identity_evidence", {})
    if not isinstance(evidence, dict) or not str(evidence.get("code", "")).strip():
        raise ValueError("Public teaching source course-code evidence missing")
    if not all(isinstance(evidence.get(key), str) for key in ("code", "title", "version")):
        raise ValueError("Source identity evidence must contain literal text")
    source_urls = re.findall(r"https://[^\s;|]+", str(binding.get("book_evidence_urls", "")))
    exact_source = record["source_url"] in source_urls
    unknown_code = binding["code"] == "No code listed" and exact_source and record.get("identity_binding") == "exact-existing-book-evidence-url-title-version"
    if not unknown_code and not code_aliases(binding["code"]).issubset(code_aliases(evidence["code"])) and not verified_renumbering(record, binding, evidence):
        raise ValueError("Public teaching course-code quote contradicts saved identity")
    if evidence["title"].strip() and normalized_anchor(evidence["title"]) != normalized_anchor(binding["title"]):
        raise ValueError("Public teaching title quote contradicts saved identity")
    undated = any(binding.get(key) == "undated" for key in ("catalog_year", "source_year")) and record.get("version_status") == "source-undated"
    if not evidence["version"].strip() and not undated:
        raise ValueError("Public teaching source version evidence missing")
    if evidence["version"].strip():
        version = binding.get("book_evidence_versions") or binding.get("offering_term") or binding.get("source_year") or binding.get("catalog_year") or binding.get("version") or binding.get("course_version")
        expected_years = set(re.findall(r"\b(?:19|20)\d{2}\b", str(version)))
        for start, end in re.findall(r"\b((?:19|20)\d{2})-(\d{2})\b", str(version)):
            expected_years.add(start[:2] + end)
        quoted_years = set(re.findall(r"\b(?:19|20)\d{2}\b", evidence["version"]))
        if not expected_years or not quoted_years or not expected_years.intersection(quoted_years):
            raise ValueError("Public teaching version quote contradicts saved identity")
        pattern = r"\b(fall|spring|summer|winter|autumn)\s+((?:19|20)\d{2})\b"
        expected_terms = set(re.findall(pattern, str(version).casefold()))
        quoted_terms = set(re.findall(pattern, evidence["version"].casefold()))
        if expected_terms and (not quoted_terms or not quoted_terms.issubset(expected_terms)):
            raise ValueError("Public teaching term quote contradicts saved identity")
    if not str(evidence.get("title", "")).strip():
        if record.get("identity_binding") != "exact-existing-book-evidence-url-and-course-code-version" or not exact_source:
            raise ValueError("Public teaching source lacks title or exact existing reading-source binding")
    return binding


def checked_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "ocw.mit.edu" or parts.port not in {None, 443} or parts.username or parts.password:
        raise ValueError("Only official HTTPS MIT OCW course sources are allowed")
    return url


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity(original):
    result = {key: str(original.get(key, "")).strip() for key in ("course_key", "title", "code", "source_year", "source_url")}
    if not all(result.values()):
        raise ValueError("Description evidence requires a complete saved course identity")
    checked_url(result["source_url"])
    return result


def landing_url(source_url):
    checked_url(source_url)
    match = re.fullmatch(r"(https://ocw\.mit\.edu/courses/[^/?#]+/)(?:pages/[^/?#]+/)?", source_url)
    if not match:
        raise ValueError("Expected an official course landing or course page URL")
    return match[1]


def parse_description(document, original, fetched_on):
    from bs4 import BeautifulSoup

    binding = identity(original)
    source_url = landing_url(binding["source_url"])
    date.fromisoformat(fetched_on)
    soup = BeautifulSoup(document.decode("utf-8"), "html.parser")
    title = soup.select_one("a.text-capitalize.m-0.text-white")
    header = soup.select_one(".course-number-term-detail")
    if title is None or header is None:
        raise ValueError("Missing official course title or version header")
    parts = header.get_text(" ", strip=True).split("|")
    year = re.search(r"\b(?:19|20)\d{2}\b", " | ".join(parts[1:]))
    if title.get_text(" ", strip=True) != binding["title"] or parts[0].strip() != binding["code"] or year is None or year[0] != binding["source_year"]:
        raise ValueError("Public course identity differs from the saved version")
    container = soup.select_one("#course-description")
    if container is None:
        raise ValueError("Missing explicitly labelled course description")
    description = container.select_one("#expanded-description") or container.select_one("#full-description")
    if description is None:
        description = container.select_one("#collapsed-description")
        if description is None or description.select_one("#expand-description") or "…" in description.get_text() or "..." in description.get_text():
            raise ValueError("Missing complete course description")
    locator = "#course-description #" + description["id"]
    for control in description.select("button, script, style"):
        control.decompose()
    text = description.get_text(" ", strip=True)
    if not text or text == binding["title"]:
        raise ValueError("Empty course teaching description")
    return {"identity": binding, "description": text, "source_url": source_url,
            "locator": locator, "fetched_on": fetched_on,
            "source_html_sha256": hashlib.sha256(document).hexdigest(),
            "description_sha256": hashlib.sha256(text.encode()).hexdigest()}


def supplement_payload(records, outcomes, inputs=None):
    ordered = sorted(records, key=lambda row: row["identity"]["course_key"])
    return {"format": FORMAT, "records": ordered, "records_sha256": digest(ordered),
            "outcomes": outcomes, "inputs": inputs or [], "knowledge_processed": False}


def validate_supplement(payload):
    if payload.get("format") != FORMAT or payload.get("knowledge_processed") is not False or not isinstance(payload.get("records"), list) or digest(payload["records"]) != payload.get("records_sha256"):
        raise ValueError("Invalid teaching-description supplement or content hash")
    result = {}
    for record in payload["records"]:
        public = record.get("evidence_type") == PUBLIC_EVIDENCE
        if record.get("evidence_type") not in {None, PUBLIC_EVIDENCE}:
            raise ValueError("Unknown teaching evidence type")
        binding = validate_public_record(record) if public else identity(record["identity"])
        text = record["description"]
        if not isinstance(text, str) or not text.strip() or hashlib.sha256(text.encode()).hexdigest() != record.get("description_sha256"):
            raise ValueError("Description passage hash does not match")
        if not public and (record.get("source_url") != landing_url(binding["source_url"]) or record.get("locator") not in {"#course-description #expanded-description", "#course-description #collapsed-description", "#course-description #full-description"}):
            raise ValueError("Description source does not match saved course version")
        if not public and not re.fullmatch(r"[a-f0-9]{64}", record.get("source_html_sha256", "")):
            raise ValueError("Description source HTML digest missing")
        date.fromisoformat(record["fetched_on"])
        key = binding["course_key"]
        if key in result:
            raise ValueError("Duplicate description source identity")
        result[key] = record
    return result


def load_description_supplement(path):
    path = Path(path)
    return validate_supplement(json.loads(path.read_text())) if path.exists() else {}


def merge_supplements(paths):
    records, outcomes, inputs = {}, [], []
    for path in map(Path, paths):
        raw = path.read_bytes()
        payload = json.loads(raw)
        for key, record in validate_supplement(payload).items():
            if key in records and records[key] != record:
                raise ValueError("Conflicting teaching descriptions for " + key)
            records[key] = record
        outcomes.extend(payload.get("outcomes", []))
        inputs.append({"path": path.name, "sha256": hashlib.sha256(raw).hexdigest()})
    result = supplement_payload(list(records.values()), outcomes, inputs)
    validate_supplement(result)
    return result


def description_variants_for(original, supplement):
    if "course_key" not in original and original.get("course_id"):
        original = original | {"course_key": "topclass-v0.4:" + original["course_id"], "code": original.get("course_code", "")}
    record = supplement.get(original.get("course_key"))
    if record is None:
        return []
    public = record.get("evidence_type") == PUBLIC_EVIDENCE
    bound = {key: original.get(key) for key in record["identity"]} if public else identity(original)
    if bound != record["identity"]:
        raise ValueError("Teaching-description supplement identity does not match inventory")
    if public:
        return [{"description": record["description"], "inventory": "teaching-description-evidence",
                 "course_key": original["course_key"], "source_urls": [record["source_url"]],
                 "source_year": record["identity"].get("source_year", record["identity"].get("catalog_year", "")),
                 "source_version": record["identity_evidence"].get("version", ""),
                 **{key: record[key] for key in ("locator", "fetched_on", "source_document_sha256", "description_sha256",
                    "identity_evidence", "source_verification_status", "version_status", "identity_binding", "evidence_kind", "code_equivalence", "source_referral", "source_code_unknown") if key in record}}]
    return [{"description": record["description"], "inventory": "teaching-description-evidence",
             "course_key": original["course_key"], "source_urls": [record["source_url"]],
             "source_year": record["identity"]["source_year"], "locator": record["locator"],
             "fetched_on": record["fetched_on"], "source_html_sha256": record["source_html_sha256"],
             "description_sha256": record["description_sha256"]}]


def captured_document(client, url, fetched_on):
    from mit_ocw_readings import checked_cache_file, save_json

    key = hashlib.sha256(url.encode()).hexdigest()
    cache_path = client.folder / (key + ".json")
    capture_path = client.folder / (key + ".capture.json")
    checked_cache_file(capture_path)
    was_cached = cache_path.exists()
    document = client.get(url)
    source_hash = hashlib.sha256(document).hexdigest()
    if was_cached:
        if not capture_path.exists():
            raise ValueError("Cached source has no original capture date; cannot invent fetched_on")
        captured = json.loads(capture_path.read_text())
        if captured.get("url") != url or captured.get("source_html_sha256") != source_hash:
            raise ValueError("Cached capture provenance does not match source HTML")
        date.fromisoformat(captured["fetched_on"])
    else:
        captured = {"url": url, "fetched_on": fetched_on, "source_html_sha256": source_hash}
        save_json(capture_path, captured, indent=2)
    return document, captured["fetched_on"]


def main(argv=None):
    from mit_ocw_readings import AccessLimit, CachedClient, load_catalog, save_json

    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--merge", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fetched-on", default=date.today().isoformat())
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=1800)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    if args.delay < 0.25 or not 1 <= args.limit <= 1800:
        parser.error("Use delay >= 0.25 seconds and limit between 1 and 1800")
    if args.out.exists():
        raise FileExistsError("Use a fresh output path; the cache supports resumable collection")
    if args.merge:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = merge_supplements(args.merge)
        save_json(args.out, payload, indent=2)
        print(json.dumps({"descriptions": len(payload["records"])}))
        return 0
    if args.inventory is None or args.cache is None:
        parser.error("Collection requires --inventory and --cache")
    date.fromisoformat(args.fetched_on)
    inventory_hash = hashlib.sha256(args.inventory.read_bytes()).hexdigest()
    courses = sorted((row for row in load_catalog(args.inventory) if not row.get("description", "").strip()), key=lambda row: row["course_key"])
    client = CachedClient(args.cache, delay=args.delay, offline=args.offline)
    records, outcomes = [], []
    stopped = False
    args.out.parent.mkdir(parents=True, exist_ok=True)
    def checkpoint(complete=False):
        if hashlib.sha256(args.inventory.read_bytes()).hexdigest() != inventory_hash:
            raise ValueError("Input inventory changed during collection")
        payload = supplement_payload(records, outcomes, [{"path": args.inventory.name, "sha256": inventory_hash}])
        payload["collection"] = {"eligible": len(courses), "attempted": len(outcomes), "complete": complete,
                                 "stopped_on_access_limit": stopped, "request_limit": args.limit,
                                 "network_requests": client.network_requests, "cached_requests": client.cached_requests}
        validate_supplement(payload)
        save_json(args.out, payload, indent=2)
        return payload
    checkpoint()
    try:
        for original in courses[:args.limit]:
            try:
                url = landing_url(identity(original)["source_url"])
                document, captured_on = captured_document(client, url, args.fetched_on)
                records.append(parse_description(document, original, captured_on))
                outcomes.append({"course_key": original["course_key"], "status": "description-saved"})
            except AccessLimit as error:
                outcomes.append({"course_key": original["course_key"], "status": "access-limit", "reason": str(error)})
                stopped = True
                break
            except (ValueError, urllib.error.URLError) as error:
                outcomes.append({"course_key": original["course_key"], "status": "unresolved", "reason": str(error)})
            if len(outcomes) % 20 == 0:
                checkpoint()
    finally:
        checkpoint()
    payload = checkpoint(complete=not stopped and len(outcomes) == len(courses))
    print(json.dumps(payload["collection"] | {"descriptions": len(records)}))
    return 2 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
