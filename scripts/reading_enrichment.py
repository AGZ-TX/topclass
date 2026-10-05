#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import re
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .catalog_identity import resolve_institution
    from .course_map import read_payload
    from .materials_ledger import canonical_isbn, normalize_text
except ImportError:
    from catalog_identity import resolve_institution
    from course_map import read_payload
    from materials_ledger import canonical_isbn, normalize_text


FORMAT = "topclass-reading-evidence-v1"
SUPPLEMENT = "topclass-reading-supplement-v1"
ROOT = Path(__file__).resolve().parents[1] / "data" / "expansion"
FOLDER = "reading-enrichment-2026-10-01"
DERIVED = "reading-materials.json.xz"
DOMAINS = {"berkeley": "berkeley.edu", "brown": "brown.edu", "carnegie-mellon": "cmu.edu",
           "columbia": "columbia.edu", "cornell": "cornell.edu", "dartmouth": "dartmouth.edu",
           "harvard": "harvard.edu", "mit": "mit.edu", "penn": "upenn.edu",
           "princeton": "princeton.edu", "stanford": "stanford.edu", "yale": "yale.edu"}
PUBLISHERS = {"cambridge.org", "oup.com", "wiley.com", "pearson.com", "routledge.com",
              "taylorfrancis.com", "springer.com", "mitpress.mit.edu", "sup.org",
              "press.princeton.edu", "press.uchicago.edu", "openstax.org", "elsevier.com",
              "mheducation.com", "cengage.com", "wwnorton.com", "macmillanlearning.com"}


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected nonempty " + field)
    return value.strip()


def plain(value):
    return " ".join(re.findall(r"\w+", normalize_text(value)))


def code(value):
    return re.sub(r"[\s-]", "", text(value, "course code")).casefold()


def supported(value, citation):
    return bool(plain(value)) and (" " + plain(value) + " ") in (" " + plain(citation) + " ")


def positive_role_evidence(role, evidence, sources):
    wording = {"required": r"\brequired\b", "optional": r"\boptional\b",
               "recommended": r"\b(?:recommended|recommendations?|suggested)\b"}[role]
    if not isinstance(evidence, str) or not plain(evidence) or not re.search(wording, plain(evidence)):
        return False
    phrase = r"(?<!\w)" + re.escape(plain(evidence)) + r"(?!\w)"
    for source in sources:
        for fragment in re.split(r"[.!?;\n]|\b(?:but|however)\b", source, flags=re.I):
            normalized = plain(fragment)
            for occurrence in re.finditer(phrase, normalized):
                for keyword in re.finditer(wording, occurrence[0]):
                    prefix = normalized[:occurrence.start() + keyword.start()]
                    if not re.search(r"\b(?:not|never|no|none|(?:isn|aren|wasn|weren|don|doesn|didn|won|wouldn|shouldn|couldn|mustn|needn)\s+t)\b", prefix):
                        return True
    return False


def code_supported(value, excerpt):
    pattern = r"[\s-]*".join(re.escape(char) for char in code(value))
    return bool(re.search(r"(?<![\w.])" + pattern + r"(?:(?![\w.])|(?=\.\s))", excerpt, re.I))


def source_identity_basis(record):
    if code_supported(record["source_course_code"], record["source_course_identity_excerpt"]):
        return "source-header"
    parsed = urlsplit(record["source_url"])
    match = re.fullmatch(r"/syllabus/([A-Za-z]+):(\d+[A-Za-z]?)(?::[^/:]+)+/?", parsed.path)
    if parsed.hostname == "coursetools.brown.edu" and match and code(match[1] + match[2]) == code(record["source_course_code"]):
        return "official-brown-syllabus-endpoint-and-header-title"
    raise ValueError("Source course code is not supported by the saved header or an approved official identity endpoint")


def checked_url(value, domains):
    parsed = urlsplit(text(value, "source_url"))
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443} or not any(
            host == domain or host.endswith("." + domain) for domain in domains):
        raise ValueError("Source URL must be HTTPS on an approved primary-source domain")
    return value


def checked_course_source(record, institution):
    university_id = institution["institution_id"]
    url = record.get("source_url")
    parsed = urlsplit(text(url, "source_url"))
    if parsed.hostname == "cmu.primo.exlibrisgroup.com":
        if university_id != "carnegie-mellon" or record.get("source_kind") != "official-library-course-reserve-metadata":
            raise ValueError("CMU library vendor metadata must be course reserves for Carnegie Mellon")
        checked_url(url, {"cmu.primo.exlibrisgroup.com"})
        if parsed.path not in {"/discovery/search", "/discovery/fulldisplay"}:
            raise ValueError("Only public CMU course-reserve discovery metadata routes are approved")
        authorization = record.get("official_source_authorization")
        if not isinstance(authorization, dict):
            raise ValueError("Vendor metadata requires official_source_authorization")
        checked_url(authorization.get("source_url"), {"cmu.edu"})
        excerpt = text(authorization.get("evidence_excerpt"), "official source authorization excerpt")
        if not re.search(r"https://cmu\.primo\.exlibrisgroup\.com(?=/|[\s\"'<]|$)", excerpt):
            raise ValueError("Official library authorization must identify the exact vendor host")
        return url
    return checked_url(url, {DOMAINS[university_id]})


def child(root, name):
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("Inventory filenames must be direct children")
    path = (root / name).resolve()
    if path.parent != root.resolve():
        raise ValueError("Inventory path escapes its registered parent")
    return path


def read_evidence(folder):
    records = []
    quarantined = []
    files = []
    dates = set()
    seen = set()
    for name in sorted(path.name for path in folder.glob("evidence-*.json")):
        candidate = folder / name
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("Reading evidence must be regular files, not links or directories")
        path = child(folder, name)
        raw = path.read_bytes()
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("format") != FORMAT or not isinstance(payload.get("evidence"), list):
            raise ValueError("Expected reading evidence format and evidence list")
        checked_on = text(payload.get("checked_on"), "checked_on")
        date.fromisoformat(checked_on)
        dates.add(checked_on)
        for field, destination in (("evidence", records), ("quarantined_evidence", quarantined)):
            collection = payload.get(field, [])
            if not isinstance(collection, list):
                raise ValueError(field + " must be a list")
            for record in collection:
                if not isinstance(record, dict):
                    raise ValueError("Each evidence record must be an object")
                eid = text(record.get("evidence_id"), "evidence_id")
                if eid in seen:
                    raise ValueError("Duplicate evidence_id: " + eid)
                seen.add(eid)
                destination.append(dict(record) | {"checked_on": checked_on, "evidence_file": name})
        quarantine_count = len(payload.get("quarantined_evidence", []))
        files.append({"file": name, "collection": "evidence", "records": len(payload["evidence"]) + quarantine_count,
                      "linked_evidence_records": len(payload["evidence"]), "quarantined_evidence_records": quarantine_count,
                      "sha256": hashlib.sha256(raw).hexdigest()})
    if not records + quarantined or len(dates) != 1:
        raise ValueError("Evidence must be nonempty and share one checked_on date")
    return records, quarantined, files, dates.pop()


def course_targets(root, records):
    index = json.loads((root / "catalog-datasets-2026-27.json").read_text())
    registered = {pair["inventory"] for pair in index["dataset_pairs"]}
    requested = defaultdict(set)
    for record in records:
        matches = record.get("matches")
        if not isinstance(matches, list) or not matches:
            raise ValueError("Evidence requires explicit course matches")
        seen = set()
        for match in matches:
            if not isinstance(match, dict):
                raise ValueError("Each course match must be an object")
            name = match.get("inventory")
            if name not in registered:
                raise ValueError("Course match inventory is not registered: " + str(name))
            key = text(match.get("course_key"), "course_key")
            if (name, key) in seen:
                raise ValueError("Duplicate declared course match")
            seen.add((name, key))
            requested[name].add(key)
    found = defaultdict(list)
    for name, keys in sorted(requested.items()):
        folder = child(root, name)
        manifest = json.loads((folder / "manifest.json").read_text())
        for entry in manifest["files"]:
            if entry.get("collection", "courses") != "courses":
                continue
            child(folder, entry["file"])
            payload = read_payload(folder, entry, "courses")
            for row in payload["courses"]:
                if row.get("course_key") in keys:
                    found[(name, row["course_key"])].append({
                        "institution": row.get("institution") or payload.get("institution") or entry.get("institution") or manifest.get("institution"),
                        "code": row.get("code") or row.get("course_code"),
                        "title": row.get("title") or row.get("course_title")})
    return found


def checked_material(material, record, match):
    if not isinstance(material, dict):
        raise ValueError("Each material citation must be an object")
    title = text(material.get("title"), "material title")
    citation = text(material.get("citation"), "exact material citation")
    bibliography = material.get("bibliographic_sources", [])
    if not isinstance(bibliography, list):
        raise ValueError("bibliographic_sources must be a list")
    for source in bibliography:
        if not isinstance(source, dict):
            raise ValueError("Each bibliographic source must be an object")
        checked_url(source.get("source_url"), PUBLISHERS)
        if not supported(title, text(source.get("citation"), "publisher citation")):
            raise ValueError("Publisher citation must identify the same book title")
    if not supported(title, citation):
        raise ValueError("Material title must occur in the course citation")
    for field in ("authors", "edition", "isbn"):
        value = material.get(field)
        if value is not None and (not isinstance(value, str) or not supported(value, citation)):
            raise ValueError("Unsupported bibliographic field: " + field)
    isbn = material.get("isbn")
    if isbn and not canonical_isbn(isbn):
        raise ValueError("ISBN checksum is invalid")
    role = material.get("assignment_role", "not-stated")
    if role not in {"required", "optional", "recommended", "not-stated"}:
        raise ValueError("Unknown assignment_role")
    role_evidence = material.get("role_evidence", "")
    if role != "not-stated":
        if not positive_role_evidence(role, role_evidence, [record["evidence_excerpt"], citation]):
            raise ValueError("Assignment role requires exact supporting role_evidence")
    temporal = "undated" if record["source_year"] == "undated" else "source-year " + record["source_year"]
    indexed = record['source_kind'] == 'official-indexed-course-reading-metadata'
    reserve = record["source_kind"] == "official-library-course-reserve-metadata"
    result = {"resource_type": material.get("resource_type", "book"), "exact_source_title_or_citation": title,
              "authors": material.get("authors"), "edition": material.get("edition"), "isbn": isbn,
              "metadata_origin": {field: ("library-bibliography" if reserve else "course-citation") if material.get(field) is not None else "not-stated"
                                  for field in ("authors", "edition", "isbn")},
              "isbn_validation": "checksum valid; bibliography remains a candidate" if isbn else "not stated",
              "assignment_role": role, "role_evidence": role_evidence, "citation": citation,
              "source_url": record["source_url"], "source_year": record["source_year"],
              "source_course_code": record["source_course_code"], "source_course_title": record["source_course_title"],
              "source_course_identity_excerpt": record["source_course_identity_excerpt"],
              "source_identity_basis": record["source_identity_basis"],
              "catalog_code": match["code"], "catalog_title": match["title"],
              "course_match_basis": match.get("match_basis", "exact-code-and-title"),
              "identity_rationale": match.get("identity_rationale", ""), "bibliographic_sources": bibliography,
              "evidence_excerpt": record["evidence_excerpt"], "evidence_location": record["evidence_location"],
              "checked_on": record["checked_on"], "evidence_id": record["evidence_id"],
              "association_status": "library-course-reserve-candidate" if reserve else "course-source-reading-candidate", "current_adoption_verified": False,
              "identity_sources": match.get("identity_sources", []),
              **({"official_source_authorization": record["official_source_authorization"]}
                 if "official_source_authorization" in record else {}),
              "bibliography_verified": False, "knowledge_processed": False,
              "evidence_scope": temporal + " official course reading evidence; current adoption is not verified"}
    if indexed:
        result['source_verification_status'] = record['source_verification_status']
        result['evidence_scope'] = temporal + ' indexed-only official reading metadata; live page and current adoption are not verified'
    if "reading_extent" in material:
        result["reading_extent"] = text(material["reading_extent"], "reading extent")
    if "additional_citations" in material:
        citations = material["additional_citations"]
        if not isinstance(citations, list) or any(not isinstance(value, str) or not supported(title, value) for value in citations):
            raise ValueError("Additional citations must identify the same source book title")
        result["additional_citations"] = list(citations)
    if reserve and "bibliographic_metadata" in record:
        bibliography = record["bibliographic_metadata"]
        if not isinstance(bibliography, dict) or any(not isinstance(key, str) or not isinstance(values, list)
                or any(not isinstance(value, str) for value in values) for key, values in bibliography.items()):
            raise ValueError("Library bibliography must preserve string-list metadata")
        result["library_bibliography"] = bibliography
    return result


def checked_record(record, *, quarantined=False):
    institution = resolve_institution(record.get("institution"))
    if not institution["in_scope"]:
        raise ValueError("Evidence institution is outside the 12-university scope")
    checked_course_source(record, institution)
    year = record.get("source_year")
    if not isinstance(year, str) or not (year == "undated" or re.fullmatch(r"\d{4}", year)):
        raise ValueError("source_year must be four digits or undated")
    for field in ("source_course_code", "source_course_identity_excerpt", "source_kind", "evidence_excerpt", "evidence_location"):
        text(record.get(field), field)
    if record['source_kind'] == 'official-indexed-course-reading-metadata' and record.get('source_verification_status') != 'indexed-only; live page not verified':
        raise ValueError('Indexed-only evidence requires explicit unverified live-page status')
    title = record.get("source_course_title")
    if title is not None or not quarantined:
        text(title, "source_course_title")
    if len(record["evidence_excerpt"].split()) > 25:
        raise ValueError("Narrative evidence_excerpt exceeds 25 words")
    header = record["source_course_identity_excerpt"]
    if title is not None and not supported(title, header):
        raise ValueError("Source course identity is not supported by the saved header")
    record["source_identity_basis"] = source_identity_basis(record)
    if not isinstance(record.get("materials"), list) or not record["materials"]:
        raise ValueError("Reading evidence requires explicit material citations")
    return institution


def checked_yale_term_suffix(record, match, institution):
    source_code = code(record["source_course_code"])
    catalog_code = code(match["code"])
    if institution["institution_id"] != "yale" or not re.fullmatch(r"[a-z]{2,}\d{4}", source_code) or catalog_code not in {source_code + "a", source_code + "b"}:
        raise ValueError("Yale term suffix requires the same four-digit subject and number with an explicit a/b catalog suffix")
    text(match.get("identity_rationale"), "identity_rationale")
    sources = match.get("identity_sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Yale term suffix requires official identity_sources")
    catalog_found = False
    convention_found = False
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Each identity mapping source must be an object")
        url = checked_url(source.get("source_url"), {"yale.edu"})
        excerpt = text(source.get("evidence_excerpt"), "identity mapping excerpt")
        parsed = urlsplit(url)
        if parsed.hostname == "catalog.yale.edu":
            catalog_found |= code_supported(match["code"], excerpt) and supported(match["title"], excerpt)
            if parsed.path.rstrip("/") == "/gsas/degree-granting-departments-programs":
                convention_found |= (supported("Fall-term courses are indicated by the letter a", excerpt)
                                     and supported("spring-term courses by the letter b", excerpt))
    if not catalog_found or not convention_found:
        raise ValueError("Yale term suffix requires the exact catalog code/title and official fall-a/spring-b convention")


def compile_evidence(root, folder):
    records, quarantined, evidence_files, checked_on = read_evidence(folder)
    targets = course_targets(root, records)
    rows = []
    for record in quarantined:
        checked_record(record, quarantined=True)
        if record.get("matches") != [] or not isinstance(record.get("gaps"), list) or not record["gaps"]:
            raise ValueError("Quarantined evidence requires empty matches and explicit nonempty gaps")
        for gap in record["gaps"]:
            text(gap, "quarantine gap")
        for material in record["materials"]:
            checked_material(material, record, {"code": "", "title": "", "match_basis": "quarantined-unlinked"})
        record["current_adoption_verified"] = False
        record["knowledge_processed"] = False
    for record in records:
        institution = checked_record(record)
        year = record["source_year"]
        header = record["source_course_identity_excerpt"]
        materials = record["materials"]
        for match in record["matches"]:
            candidates = targets.get((match["inventory"], match["course_key"]), [])
            if not any(resolve_institution(row["institution"])["institution_id"] == institution["institution_id"]
                       and code(row["code"]) == code(match.get("code")) and plain(row["title"]) == plain(match.get("title")) for row in candidates):
                raise ValueError("Unknown or conflicting saved course match: " + match["course_key"])
            basis = match.get("match_basis", "exact-code-and-title")
            if basis == "official-yale-term-suffix":
                checked_yale_term_suffix(record, match, institution)
            elif basis in {"official-renumbering", "official-code-alias"}:
                text(match.get("identity_rationale"), "identity_rationale")
                identity_sources = match.get("identity_sources")
                if not isinstance(identity_sources, list) or not identity_sources:
                    raise ValueError("Explicit code mappings require official identity_sources")
                mapping_found = False
                for source in identity_sources:
                    if not isinstance(source, dict):
                        raise ValueError("Each identity mapping source must be an object")
                    checked_url(source.get("source_url"), {DOMAINS[institution["institution_id"]]})
                    excerpt = text(source.get("evidence_excerpt"), "identity mapping excerpt")
                    mapping_found |= code_supported(match["code"], excerpt) and code_supported(record["source_course_code"], excerpt)
                if not mapping_found:
                    raise ValueError("Official mapping excerpt must support both course codes")
            elif code(match["code"]) != code(record["source_course_code"]):
                raise ValueError("Source and catalog course codes do not match")
            if basis == "exact-code-and-title":
                if plain(match["title"]) != plain(record["source_course_title"]):
                    raise ValueError("Source and catalog titles differ without an explicit title-variant rationale")
            elif basis == "official-code-with-title-variant":
                text(match.get("identity_rationale"), "identity_rationale")
            elif basis not in {"official-renumbering", "official-code-alias", "official-yale-term-suffix"}:
                raise ValueError("Unsupported course match basis")
            gaps = record.get("gaps", [])
            if not isinstance(gaps, list) or any(not isinstance(value, str) for value in gaps):
                raise ValueError("Evidence gaps must be explicit strings")
            rows.append({"course_key": match["course_key"], "institution": record["institution"],
                         "code": match["code"], "title": match["title"], "status": "found",
                         "source_url": record["source_url"], "source_year": year, "source_kind": record["source_kind"],
                         "source_course_identity_excerpt": header, "evidence_excerpt": record["evidence_excerpt"],
                         "evidence_location": record["evidence_location"], "checked_on": checked_on,
                         "evidence_id": record["evidence_id"], "inventory": match["inventory"],
                         "identity_sources": match.get("identity_sources", []),
                         **({"official_source_authorization": record["official_source_authorization"]}
                            if "official_source_authorization" in record else {}),
                         "reading_source_inspected": record['source_kind'] != 'official-indexed-course-reading-metadata',
                         "reading_source_scope": 'official-indexed-reading-evidence' if record['source_kind'] == 'official-indexed-course-reading-metadata' else "official-named-reading-evidence", "reading_list_complete": False,
                         "current_adoption_verified": False, "gaps": gaps + ["Current adoption and exact course-version equivalence remain unverified."],
                         "materials": [checked_material(item, record, match) for item in materials]})
    rows.sort(key=lambda row: (row["course_key"], row["evidence_id"], row["inventory"]))
    payload = {"format": SUPPLEMENT, "checked_on": checked_on, "knowledge_processed": False,
               "materials": rows, "quarantined_evidence": quarantined}
    packed = lzma.compress(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    manifest = {"format": SUPPLEMENT, "checked_on": checked_on, "evidence_records": len(records),
                "quarantined_evidence_records": len(quarantined),
                "materials_rows": len(rows), "material_records": sum(len(row["materials"]) for row in rows),
                "current_adoption_verified": False, "knowledge_processed": False,
                "files": evidence_files + [{"file": DERIVED, "collection": "materials", "records": len(rows),
                                            "compressed_sha256": hashlib.sha256(packed).hexdigest(), "compressed_bytes": len(packed)}]}
    return manifest, packed


def checked_outputs(folder):
    for name in (DERIVED, "manifest.json"):
        destination = folder / name
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ValueError("Derived outputs must be regular files, not links or directories")


def build(root=ROOT, folder=None, *, replace=False):
    root = Path(root).resolve()
    folder = Path(folder or root / FOLDER).resolve()
    if folder.parent != root or folder.name != FOLDER:
        raise ValueError("Enrichment output must use its dedicated registered expansion folder")
    checked_outputs(folder)
    manifest, packed = compile_evidence(root, folder)
    if (folder / DERIVED).exists() or (folder / "manifest.json").exists():
        if not replace:
            raise FileExistsError("Derived supplement already exists; use --replace")
        existing = json.loads((folder / "manifest.json").read_text())
        if existing.get("format") != SUPPLEMENT:
            raise ValueError("Refuse to replace an unregistered derived supplement")
    with tempfile.TemporaryDirectory(prefix=".reading-enrichment-", dir=folder) as temporary:
        staged = Path(temporary)
        (staged / DERIVED).write_bytes(packed)
        (staged / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        checked_outputs(folder)
        installed = []
        try:
            for name in (DERIVED, "manifest.json"):
                destination = folder / name
                if replace and destination.exists():
                    os.replace(destination, staged / (name + ".previous"))
                if replace:
                    os.replace(staged / name, destination)
                else:
                    os.link(staged / name, destination)
                installed.append(name)
        except BaseException:
            for name in reversed((DERIVED, "manifest.json")):
                previous = staged / (name + ".previous")
                destination = folder / name
                if previous.exists():
                    os.replace(previous, destination)
                elif name in installed:
                    destination.unlink()
            raise
    return manifest


def verify(root=ROOT, folder=None):
    root = Path(root).resolve()
    folder = Path(folder or root / FOLDER).resolve()
    manifest = json.loads((folder / "manifest.json").read_text())
    for entry in manifest.get("files", []):
        raw = child(folder, entry["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != (entry.get("sha256") or entry.get("compressed_sha256")):
            raise ValueError("SHA-256 mismatch: " + entry["file"])
    expected, packed = compile_evidence(root, folder)
    if manifest != expected or (folder / DERIVED).read_bytes() != packed:
        raise ValueError("Derived reading supplement counts or evidence do not reproduce")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Validate and build official reading evidence without network access.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--folder", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("build")
    command.add_argument("--replace", action="store_true")
    sub.add_parser("verify")
    args = parser.parse_args()
    try:
        result = build(args.root, args.folder, replace=args.replace) if args.command == "build" else verify(args.root, args.folder)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, "ERROR: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
