#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import lzma
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from bs4 import BeautifulSoup

from materials_ledger import canonical_isbn
from reading_enrichment import code_supported, plain, positive_role_evidence, supported

ROOT = Path(__file__).resolve().parents[1]
MAX_RESPONSE_BYTES = 3_000_000
PUBLISHER = re.compile(r"University Press|\bPress\b|Springer|McGraw|Wiley|Pearson|Prentice|Penguin|Dover|Routledge|Harper|Elsevier|Addison|Norton|Oxford|Cambridge|Macmillan|Knopf|Random House|MIT Press|Chapman|CRC|Grove|Houghton|Simon|Little, Brown|Basic Books|Vintage|Scribner|Bedford|Freeman|Benjamin|O'Reilly|Morgan Kaufmann", re.I)
JOURNAL = re.compile(r"\bJournal\b|\bProceedings\b|\bReview\b|\bTransactions\b|^Science$|^Nature$|^Econometrica$|^American Economic", re.I)
ISBN = re.compile(r"ISBN(?:-1[03])?\s*[:\-]?\s*([\dXx][\dXx -]{8,}[\dXx])", re.I)
EDITION = re.compile(r"\b(?:\d+\s*(?:st|nd|rd|th)?\s*(?:ed\.|edition)|(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Revised|Reprint|Abridged) edition)", re.I)


def save_json(path, value, indent=None):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix="." + path.name, delete=False) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=indent)
        temporary.write("\n")
        staged = Path(temporary.name)
    staged.replace(path)


class AccessLimit(RuntimeError):
    pass


class CacheIntegrityError(AccessLimit, ValueError):
    pass


def checked_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "ocw.mit.edu" or parts.port not in {None, 443} or parts.username or parts.password:
        raise ValueError("Only HTTPS URLs on the official public OCW host are allowed")
    return url


class ScopedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        checked_url(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


class CachedClient:
    def __init__(self, folder, delay=0.25, fetcher=None, offline=False):
        self.folder = Path(folder)
        if self.folder.is_symlink() or any(parent.is_symlink() for parent in self.folder.parents) or (self.folder.exists() and not self.folder.is_dir()):
            raise ValueError("Metadata cache must use regular directories without symlinks")
        self.folder.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.fetcher = fetcher or self.fetch
        self.offline = offline
        self.legacy_cache_urls = set()
        self.network_requests = 0
        self.cached_requests = 0
        self.stopped = threading.Event()
        self.stop_path = self.folder / "access-limit.json"
        checked_cache_file(self.stop_path)
        if self.stop_path.exists():
            self.stopped.set()

    def fetch(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": "Topclass official course-reading metadata research/1.0", "Accept": "text/html,application/xml"})
        opener = urllib.request.build_opener(ScopedRedirectHandler())
        with opener.open(request, timeout=25) as response:
            checked_url(response.geturl())
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xml", "text/xml"}:
                raise ValueError("Public source is not an allowed HTML/XML metadata response")
            content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeds configured metadata limit")
        return content

    def get(self, url):
        checked_url(url)
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.folder / (key + ".json")
        checked_cache_file(path)
        if path.exists():
            cached = json.loads(path.read_text())
            if cached["url"] != url:
                self.integrity_stop(url, "Cache URL does not match")
            self.cached_requests += 1
            if cached.get("error"):
                if cached["error"] in {401, 403, 429}:
                    self.stopped.set()
                    save_json(self.stop_path, {"url": url, "status": cached["error"]})
                    raise AccessLimit("Cached public-source access limit; stopped: " + url)
                raise urllib.error.HTTPError(url, cached["error"], "Cached public-source failure", None, None)
            content = cached["content"].encode()
            if "content_sha256" in cached:
                if hashlib.sha256(content).hexdigest() != cached["content_sha256"]:
                    self.integrity_stop(url, "Metadata cache content hash does not match")
            else:
                self.legacy_cache_urls.add(url)
            return content
        if self.stopped.is_set():
            raise AccessLimit("Stopped after a public-source access limit")
        if self.offline:
            raise ValueError("Offline metadata replay has no cached source response: " + url)
        try:
            self.network_requests += 1
            body = self.fetcher(url)
            content = body.decode("utf-8", errors="replace")
            save_json(path, {"url": url, "content": content, "content_sha256": hashlib.sha256(content.encode()).hexdigest()})
            return body
        except urllib.error.HTTPError as error:
            save_json(path, {"url": url, "error": error.code})
            if error.code in {401, 403, 429}:
                self.stopped.set()
                save_json(self.stop_path, {"url": url, "status": error.code})
                raise AccessLimit(f"Public source returned {error.code}; stopped: {url}") from error
            raise
        finally:
            time.sleep(self.delay)

    def integrity_stop(self, url, message):
        self.stopped.set()
        save_json(self.stop_path, {"url": url, "integrity_failure": message})
        raise CacheIntegrityError(message)


def checked_cache_file(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Metadata checkpoint must be a regular file without symlinks")


def save_new_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def discover_links(document, source_url, course_url):
    prefix = course_url.rstrip("/") + "/pages/"
    links = set()
    for link in BeautifulSoup(document, "html.parser").find_all("a", href=True):
        url = urllib.parse.urljoin(source_url, link["href"]).split("#")[0].split("?")[0]
        text = link.get_text(" ", strip=True)
        if url.startswith(prefix) and re.search(r"reading|bibliograph|textbook", text + " " + url, re.I) and url.endswith("/"):
            links.add(url)
    return sorted(links)


def discover_candidates(sitemap, courses, mappings=None, *, all_versions=False):
    catalog = {row["code"].casefold(): row for row in courses}
    renumbering = {row["oldnum"].casefold(): row for row in mappings or [] if not row.get("removed") and row["newnum"].casefold() in catalog}
    candidates = {}
    for element in ET.fromstring(sitemap).iter():
        if not element.tag.endswith("loc") or not element.text:
            continue
        match = re.fullmatch(r"https://ocw\.mit\.edu/courses/((\w+)-([\da-z]+)-.+-(?:spring|summer|fall|january-iap|winter)-(\d{4}))/sitemap\.xml", element.text, re.I)
        if not match:
            continue
        source_code = match[2].upper() + "." + match[3].upper()
        mapping = renumbering.get(source_code.casefold())
        row = catalog.get(source_code.casefold()) or (catalog.get(mapping["newnum"].casefold()) if mapping else None)
        if row is None:
            continue
        candidate = {"url": "https://ocw.mit.edu/courses/" + match[1], "year": int(match[4]),
                     "row": {key: row[key] for key in ("code", "title", "course_key")}}
        if mapping and source_code.casefold() != row["code"].casefold():
            candidate.update(source_course_code=mapping["oldnum"], mapping_title=mapping["title"],
                             identity_mapping_source={"source_url": "https://eecsis.mit.edu/numbering.html", "evidence_excerpt": mapping["newnum"] + " [" + mapping["oldnum"] + "] " + mapping["title"]})
        candidate_identity = (row["course_key"], candidate["url"]) if all_versions else row["course_key"]
        prior = candidates.get(candidate_identity)
        if prior is None or (candidate["year"], candidate["url"]) > (prior["year"], prior["url"]):
            candidates[candidate_identity] = candidate
    return sorted(candidates.values(), key=lambda candidate: (candidate["row"]["code"], candidate["year"], candidate["url"]))


def load_catalog(path):
    body = path.read_bytes()
    payload = json.loads(lzma.decompress(body) if path.suffix == ".xz" else body)
    return payload["courses"] if isinstance(payload, dict) else payload


def archive_target(candidate, page, catalog, mappings=None):
    code, title = page["source_course_code"].strip(), page["source_course_title"].strip()
    if not re.fullmatch(r"[A-Za-z0-9]+\.[A-Za-z0-9][A-Za-z0-9.-]*", code) or not title or code.upper().startswith("RES."):
        raise ValueError("Source does not expose one supported academic subject code and nonempty title")
    matches = [row for row in catalog if plain(row["code"]) == plain(code) and plain(row["title"]) == plain(title)]
    if len(matches) == 1:
        return {key: matches[0][key] for key in ("code", "title", "course_key")}, None
    for mapping in mappings or []:
        if mapping.get("removed") or plain(mapping["oldnum"]) != plain(code) or plain(mapping["title"]) != plain(title):
            continue
        matches = [row for row in catalog if plain(row["code"]) == plain(mapping["newnum"]) and plain(row["title"]) == plain(title)]
        if len(matches) == 1:
            row = {key: matches[0][key] for key in ("code", "title", "course_key")}
            row.update(source_course_code=code, mapping_title=title,
                       identity_mapping_source={"source_url": "https://eecsis.mit.edu/numbering.html",
                                                "evidence_excerpt": mapping["newnum"] + " [" + code + "] " + title})
            return row, None
    source_year = page.get("source_course_year") or str(candidate["year"])
    key = "mit-ocw:" + hashlib.sha256(json.dumps([candidate["url"], code, title, int(source_year)], ensure_ascii=False).encode()).hexdigest()[:20]
    row = {"course_key": key, "code": code, "title": title, "inventory": "mit-ocw-course-archives-2026-10-01"}
    archive = {"institution": "Massachusetts Institute of Technology", "course_key": key, "course_record_id": key,
               "code": code, "title": title, "subject_code": code.split(".")[0],
               "description": "", "source_year": source_year, "catalog_year": source_year,
               "source_url_year": str(candidate["year"]), "source_course_header": page.get("source_course_header", code),
               "source_year_basis": "printed-course-header" if page.get("source_course_year") else "official-course-source-url",
               "source_url": candidate["url"].rstrip("/") + "/pages/syllabus/", "source_urls": [candidate["url"].rstrip("/") + "/pages/syllabus/"],
               "offering_status": "historical-official-course-archive", "catalog_status": "Official historical MIT OpenCourseWare course version",
               "materials_research_status": "Official HTML syllabus and linked bibliography inspection", "knowledge_processed": False,
               "degree_role": "Not mapped to a specific degree requirement",
               "historical_identity_note": "This official archived course version does not establish equivalence to a current same-code catalog subject or current offering."}
    return row, archive


def archive_candidates(sitemap):
    candidates = {}
    for element in ET.fromstring(sitemap).iter():
        if not element.tag.endswith("loc") or not element.text:
            continue
        match = re.fullmatch(r"https://ocw\.mit\.edu/courses/([^/]+-(?:spring|summer|fall|january-iap|winter)-(\d{4}))/sitemap\.xml", element.text, re.I)
        if match:
            url = "https://ocw.mit.edu/courses/" + match[1]
            candidates[url] = {"url": url, "year": int(match[2])}
    return sorted(candidates.values(), key=lambda candidate: candidate["url"])


def material_from_element(element, heading):
    citation = element.get_text(" ", strip=True)
    titles = list(dict.fromkeys(item.get_text(" ", strip=True).strip(" .,") for item in element.find_all(["em", "i"]) if item.get_text(strip=True)))
    if not titles or len(citation) > 4000:
        return None
    if len(titles) != 1:
        return None
    title = titles[0]
    if not title:
        return None
    isbn = ISBN.search(citation)
    edition = EDITION.search(citation)
    prefix, _, suffix = citation.partition(title)
    is_container = bool(re.search(r"\bIn\s*$", prefix))
    if JOURNAL.search(title) and not isbn:
        return None
    if re.search(r"[“\"][^”\"]+[”\"]", prefix) and not is_container:
        return None
    year = bool(re.search(r"\b(?:18|19|20)\d{2}\b", suffix))
    publication = bool(PUBLISHER.search(suffix))
    book_heading = bool(re.search(r"\bbooks?\b|textbooks?|texts\b", heading, re.I))
    author = prefix.strip(" ,.;:[]0123456789")
    if not isbn and not (publication and year) and not (book_heading and edition and author):
        return None
    if is_container or not author or re.search(r"required|recommended|optional|textbook|following|chapter|we use|we will|\bthe\b|\bit\b|\b(?:excerpt|from|sections?|models|background|overview|readings?|supplementary)\b|\bof\b|——|^by\b", author, re.I):
        author = None
    elif len(author) > 200 or ":" in author:
        author = None
    else:
        author = re.sub(r"\s*\(?\d{4}\)?\.?$", "", author).strip(" ,.") or None
    by_author = re.match(r"\s+by\s+([^,.;]+)", suffix)
    if by_author:
        author = by_author[1].strip()
    role, role_evidence = "not-stated", ""
    local_roles = {}
    local_context = prefix + " " + suffix
    for word in re.finditer(r"\b(required|optional|recommended|suggested)\b", local_context, re.I):
        candidate = "recommended" if word[0].lower() == "suggested" else word[0].lower()
        before, after = local_context[:word.start()], local_context[word.end():]
        isolated_descriptor = bool(re.search(r"(?:^|[.!?;\[(])\s*$", before) and re.match(r"\s*(?:[:)\].!\-]|$)", after))
        assignment_wording = bool(re.search(r"\b(?:is|are|be|as|considered|listed)\s*$", before)
                                  or re.match(r"\s+(?:books?|textbooks?|texts?|readings?)\s*[:\-]", after))
        if (isolated_descriptor or assignment_wording) and positive_role_evidence(candidate, word[0], [local_context]):
            local_roles[candidate] = word[0]
    if len(local_roles) == 1:
        role, role_evidence = next(iter(local_roles.items()))
    elif not local_roles and not re.search(r"\b(?:not|never|no|none|(?:isn|aren|wasn|weren|don|doesn|won|can)['’]t)\b[^.!?;]*\b(?:required|optional|recommended)\b", local_context, re.I):
        heading_roles = [candidate for candidate in ("required", "optional", "recommended") if positive_role_evidence(candidate, heading, [heading])]
        if len(heading_roles) == 1:
            role, role_evidence = heading_roles[0], heading
    source_isbn = isbn[1].strip() if isbn else None
    return {"title": title, "authors": author if author and supported(author, citation) else None,
            "edition": edition[0] if edition and supported(edition[0], citation) else None,
            "isbn": source_isbn if source_isbn and canonical_isbn(source_isbn) else None, "assignment_role": role,
            "citation": citation, "role_evidence": role_evidence,
            "reading_extent": "chapter-or-excerpt" if is_container or re.search(r"\b(?:chapters?|excerpts?)\b|\bpp?\.\s*\d+", prefix + " " + suffix, re.I) else "book-citation-extent-not-stated"}


def parse_page(document):
    soup = BeautifulSoup(document, "html.parser")
    main = soup.find("main", id="course-content-section")
    number = soup.select_one(".course-number-term-detail")
    title = soup.select_one("a.text-capitalize.m-0.text-white")
    if main is None or number is None or title is None:
        raise ValueError("Missing official course identity or main reading content")
    materials, seen, unparsed = [], {}, 0
    headings = []
    for element in main.find_all(["p", "li", "td"]):
        if element.find(["p", "li"]):
            continue
        heading_element = element.find_previous(["h2", "h3", "h4"])
        heading = heading_element.get_text(" ", strip=True) if heading_element else ""
        item = material_from_element(element, heading)
        if item:
            key = (plain(item["title"]), item["isbn"], plain(item["authors"] or ""), plain(item["edition"] or ""), item["assignment_role"])
            if key not in seen:
                seen[key] = item
                materials.append(item)
                if heading and heading not in headings:
                    headings.append(heading)
            elif item["citation"] != seen[key]["citation"]:
                citations = seen[key].setdefault("additional_citations", [])
                if item["citation"] not in citations:
                    citations.append(item["citation"])
        elif len(element.find_all(["em", "i"])) > 1:
            unparsed += 1
    header = number.get_text(" ", strip=True)
    header_parts = header.split("|")
    source_year = re.search(r"\b(?:19|20)\d{2}\b", " | ".join(header_parts[1:]))
    return {"source_course_code": header_parts[0].strip(), "source_course_header": header,
            "source_course_year": source_year[0] if source_year else None,
            "source_course_title": title.get_text(" ", strip=True), "materials": materials,
            "headings": headings, "unparsed_bibliographic_entries": unparsed}


def build_record(candidate, url, page):
    row = candidate["row"]
    source_title = page["source_course_title"]
    exact = plain(source_title) == plain(row["title"])
    def stylistic_title(title):
        return re.sub(r"^(?:intro|introductory|introduction)(?: to)?\s+", "introduction to ", plain(title))
    compatible = stylistic_title(source_title) == stylistic_title(row["title"])
    code_matches = plain(page["source_course_code"]) == plain(row["code"])
    mapping = candidate.get("identity_mapping_source")
    if mapping:
        mapping_title_matches = (stylistic_title(source_title) == stylistic_title(candidate.get("mapping_title", ""))
                                 == stylistic_title(row["title"]))
        excerpt = mapping.get("evidence_excerpt", "")
        code_matches = (mapping.get("source_url") == "https://eecsis.mit.edu/numbering.html"
                        and plain(page["source_course_code"]) == plain(candidate.get("source_course_code", ""))
                        and code_supported(row["code"], excerpt) and code_supported(page["source_course_code"], excerpt))
        compatible = mapping_title_matches
    match = {"inventory": row.get("inventory", "mit-catalog-2026-27"), "course_key": row["course_key"],
             "code": row["code"], "title": row["title"], "match_basis": "exact-code-and-title"}
    if not exact:
        match.update(match_basis="official-code-with-title-variant", identity_rationale="The official OCW and catalog sources state the same subject number and title; only the Intro/Introductory/Introduction-to prefix differs. Historical course-version equivalence and current adoption remain unverified.")
    if mapping:
        match.update(match_basis="official-renumbering", identity_rationale="The official MIT EECS subject-number conversion lists this exact old and new code. Source and mapping or catalog titles support the course identity; historical and current course versions may still differ.", identity_sources=[mapping])
    key = hashlib.sha256(url.encode()).hexdigest()[:12]
    gaps = ["Historical or undated OCW readings do not establish current textbook adoption.",
            "Only explicit HTML book bibliographies are collected; linked PDFs, lectures, papers, and unparsed citations are not claimed as covered.",
            "A parent book named in a chapter or excerpt citation does not establish assignment of the whole book; the exact citation preserves the reading extent."]
    if not code_matches or not compatible:
        gaps.append("Source identity differs from the saved catalog code or title; no official equivalence mapping was established.")
    if page["unparsed_bibliographic_entries"]:
        gaps.append(f'{page["unparsed_bibliographic_entries"]} HTML entries contain multiple italicized titles and require manual review.')
    role_excerpts = list(dict.fromkeys(item["role_evidence"] for item in page["materials"] if item["role_evidence"]))
    excerpt = "; ".join(role_excerpts) if role_excerpts else (page["headings"][0] if page["headings"] else "Official HTML book bibliography")
    if len(excerpt.split()) > 25:
        excerpt = "Official HTML book bibliography"
        for item in page["materials"]:
            if item["role_evidence"] and item["role_evidence"] not in item["citation"]:
                item["assignment_role"], item["role_evidence"] = "not-stated", ""
    return {"evidence_id": "mit-ocw-bulk-" + row["code"].lower().replace(".", "-") + "-" + key,
            "institution": "Massachusetts Institute of Technology", "source_url": url,
            "source_title": "Reading metadata | " + source_title + " | MIT OpenCourseWare",
            "source_course_code": page["source_course_code"], "source_course_title": source_title,
            "source_year": str(candidate["year"]), "source_course_identity_excerpt": page["source_course_code"] + " | " + source_title,
            "source_kind": "official-ocw-html-reading-list", "evidence_excerpt": excerpt,
            "evidence_location": "Explicit bibliography entries in the official HTML syllabus or linked course reading list",
            "matches": [match] if code_matches and compatible else [], "materials": page["materials"], "gaps": gaps}


def research(candidate, client):
    source_url = candidate["url"].rstrip("/") + "/pages/syllabus/"
    pages, outcomes = [], []
    try:
        document = client.get(source_url)
        links = discover_links(document, source_url, candidate["url"])
    except AccessLimit:
        raise
    except (OSError, ValueError) as error:
        return [], [{"course_key": candidate["row"]["course_key"], "source_url": source_url, "status": "source-unavailable", "detail": str(error)}]
    for url in [source_url] + [link for link in links if link != source_url]:
        try:
            body = document if url == source_url else client.get(url)
            page = parse_page(body)
            record = build_record(candidate, url, page)
            if candidate.get("header_year_preferred"):
                record["source_year"] = page.get("source_course_year") or str(candidate["year"])
                record["source_url_year"] = str(candidate["source_url_year"])
                record["source_course_header"] = page["source_course_header"]
                record["source_year_basis"] = "printed-course-header" if page.get("source_course_year") else "official-course-source-url"
                if record["source_year"] != record["source_url_year"]:
                    record["gaps"].append("The printed course-header year differs from the source URL ending year; the printed year is retained, and the original URL year remains separate.")
            if page["materials"]:
                record["source_capture_sha256"] = hashlib.sha256(body).hexdigest()
                pages.append(record)
            outcomes.append({"course_key": candidate["row"]["course_key"], "source_url": url,
                             "status": "named-books" if page["materials"] else "inspected-without-parsed-books",
                             "book_mentions": len(page["materials"]), "unparsed_bibliographic_entries": page["unparsed_bibliographic_entries"],
                             "source_course_code": page["source_course_code"], "source_course_title": page["source_course_title"],
                             "source_capture_sha256": hashlib.sha256(body).hexdigest(),
                             "source_year": record["source_year"], "catalog_identity_supported": bool(record and record["matches"])})
        except AccessLimit:
            raise
        except (OSError, ValueError) as error:
            outcomes.append({"course_key": candidate["row"]["course_key"], "source_url": url, "status": "source-unavailable", "detail": str(error)})
    return pages, outcomes


def research_archive(candidate, client, catalog, mappings=None):
    url = candidate["url"].rstrip("/") + "/pages/syllabus/"
    try:
        page = parse_page(client.get(url))
        row, archive = archive_target(candidate, page, catalog, mappings)
    except AccessLimit:
        raise
    except (OSError, ValueError) as error:
        return [], [{"source_url": url, "status": "source-identity-unavailable", "detail": str(error)}], None
    mapped_candidate = candidate | {"row": row, "source_url_year": candidate["year"], "header_year_preferred": True}
    for key in ("source_course_code", "mapping_title", "identity_mapping_source"):
        if key in row:
            mapped_candidate[key] = row[key]
    records, outcomes = research(mapped_candidate, client)
    for record in records:
        record["evidence_id"] = "mit-ocw-archive-" + hashlib.sha256((row["course_key"] + "\0" + record["source_url"]).encode()).hexdigest()[:24]
    if archive:
        archive["materials_research_status"] = "named-books" if any(record["matches"] for record in records) else "inspected-without-parsed-books"
    return records, outcomes, archive


def load_archive_results(path, fingerprint, *, reparse=False):
    checked_cache_file(path)
    if reparse or not path.exists():
        return {}
    payload = json.loads(path.read_text())
    if payload.get("format") != "topclass-ocw-archive-checkpoint-v1" or payload.get("fingerprint") != fingerprint or not isinstance(payload.get("results"), dict):
        raise ValueError("Archive result checkpoint inputs or parser changed; explicit --reparse is required")
    if hashlib.sha256(json.dumps(payload["results"], sort_keys=True, ensure_ascii=False).encode()).hexdigest() != payload.get("results_sha256"):
        raise ValueError("Archive result checkpoint content hash does not match")
    return payload["results"]


def validate_archive_paths(output, archive, cache):
    if any(path.is_symlink() or any(parent.is_symlink() for parent in path.parents) for path in (output, archive, cache)):
        raise ValueError("Archive outputs and cache must not use symlink paths")
    paths = [path.resolve() for path in (output, archive, cache)]
    for index, path in enumerate(paths):
        for other in paths[index + 1:]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError("Historical inventory output, evidence output, and persistent cache must not overlap")


def harvest_archives(candidates, catalog, mappings, client, results_path, previous, *, reparse=False, workers=2, input_hashes=None):
    fingerprint = hashlib.sha256(json.dumps({"candidates": candidates, "catalog": catalog, "mappings": mappings, "previous": previous,
                                             "input_sha256": input_hashes, "parser_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    results = load_archive_results(results_path, fingerprint, reparse=reparse)
    wanted = {candidate["url"] for candidate in candidates}
    results = {key: value for key, value in results.items() if key in wanted}
    pending = [candidate for candidate in candidates if candidate["url"] not in results]
    stopped = None
    def checkpoint():
        save_json(results_path, {"format": "topclass-ocw-archive-checkpoint-v1", "fingerprint": fingerprint, "results": results,
                                 "results_sha256": hashlib.sha256(json.dumps(results, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(research_archive, candidate, client, catalog, mappings): candidate for candidate in pending}
        for future in concurrent.futures.as_completed(futures):
            candidate = futures[future]
            try:
                records, outcomes, archive = future.result()
                results[candidate["url"]] = {"evidence": records, "outcomes": outcomes, "archive": archive}
                if len(results) % 20 == 0:
                    checkpoint()
                    print(json.dumps({"completed_archives": len(results), "candidates": len(candidates)}), flush=True)
            except AccessLimit as error:
                stopped = str(error)
                for remaining in futures:
                    remaining.cancel()
                break
    checkpoint()
    previous_links = {(row["source_url"], match["course_key"]) for row in previous for match in row["matches"]}
    previous_versions = {row["source_url"].split("/pages/")[0] for row in previous}
    evidence, quarantine, courses, outcomes = [], [], [], []
    for url, result in sorted(results.items()):
        archive = result["archive"]
        for row in result["evidence"]:
            if row["matches"] and all((row["source_url"], match["course_key"]) in previous_links for match in row["matches"]):
                continue
            if archive and url in previous_versions:
                continue
            (evidence if row["matches"] else quarantine).append(row)
        if archive and url not in previous_versions:
            courses.append(archive)
        outcomes.extend(result["outcomes"])
    incomplete = [{"source_url": candidate["url"], "status": "not-requested-global-stop"} for candidate in candidates if candidate["url"] not in results]
    outcomes.extend(incomplete)
    return evidence, quarantine, courses, outcomes, stopped, len(results)


def write_archive_snapshot(folder, archive_folder, candidates, courses, evidence, quarantine, outcomes, stopped, completed, checked_on, inputs, client=None, workers=2, captured_inputs=None):
    captured_inputs = captured_inputs if captured_inputs is not None else [path.read_bytes() for path in inputs]
    if len(captured_inputs) != len(inputs):
        raise ValueError("Source input byte snapshots do not match declared inputs")
    for path in (folder, archive_folder):
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents) or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
            raise FileExistsError("Source output changed during acquisition; use fresh empty output folders")
    folder.mkdir(parents=True, exist_ok=True)
    archive_folder.mkdir(parents=True, exist_ok=True)
    save_new_json(folder / "candidates.json", candidates)
    save_new_json(folder / "evidence.json", {"format": "topclass-reading-evidence-v1", "checked_on": checked_on,
                                          "evidence": evidence, "quarantined_evidence": quarantine})
    save_new_json(folder / "source-outcomes.json", {"checked_on": checked_on, "outcomes": outcomes})
    with (folder / "public-sitemap.xml").open("xb") as handle:
        handle.write(captured_inputs[0])
    packed = lzma.compress((json.dumps({"institution": "Massachusetts Institute of Technology", "courses": courses}, ensure_ascii=False) + "\n").encode())
    with (archive_folder / "courses.json.xz").open("xb") as handle:
        handle.write(packed)
    summary = {"checked_on": checked_on, "institution": "Massachusetts Institute of Technology",
               "source_kind": "historical-official-course-archive", "source_url": "https://ocw.mit.edu/sitemap.xml",
               "scope": "All dated academic OCW course versions exposed by the public sitemap. Historical source identities are separate from current catalog subjects; no term schedules or current-adoption claim.",
               "candidate_count": len(candidates), "completed_candidates": completed, "source_pages": len(outcomes),
               "linked_sources": len(evidence), "linked_course_keys": len({match["course_key"] for row in evidence for match in row["matches"]}),
               "book_mentions": sum(len(row["materials"]) for row in evidence), "historical_course_rows": len(courses),
               "quarantined_sources": len(quarantine), "stopped": stopped,
               "metadata_access": {"protocol": "Public HTTPS HTML/XML metadata only", "maximum_response_bytes": MAX_RESPONSE_BYTES,
                                   "timeout_seconds": 25, "workers": workers, "delay_seconds": client.delay if client else 0.25,
                                   "global_stop_statuses": [401, 403, 429], "offline_reproduction": client.offline if client else False,
                                   "network_requests": client.network_requests if client else None,
                                   "cached_requests": client.cached_requests if client else None,
                                   "legacy_unhashed_cache_entries_reused": len(client.legacy_cache_urls) if client else None,
                                   "legacy_cache_policy": "Legacy source checkpoints are reused unchanged and explicitly recorded; new checkpoints carry validated content hashes. Source outcome hashes bind the exact bytes parsed in this replay."},
               "inputs": [{"path": str(path), "sha256": hashlib.sha256(body).hexdigest()} for path, body in zip(inputs, captured_inputs)],
               "files": [{"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in (folder / "candidates.json", folder / "evidence.json", folder / "source-outcomes.json", folder / "public-sitemap.xml")]}
    save_new_json(folder / "manifest.json", summary)
    inventory = {"institution": "Massachusetts Institute of Technology", "checked_on": checked_on,
                 "source_kind": "historical-official-course-archive", "scope": summary["scope"],
                 "catalog_name": "MIT OpenCourseWare historical academic course versions", "catalog_url": summary["source_url"],
                 "catalog_year": "mixed historical source years", "not_a_term_schedule": True,
                 "files": [{"file": "courses.json.xz", "collection": "courses", "institution": "Massachusetts Institute of Technology",
                            "records": len(courses), "compressed_bytes": len(packed), "compressed_sha256": hashlib.sha256(packed).hexdigest()}]}
    save_new_json(archive_folder / "manifest.json", inventory)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect official MIT OCW HTML book metadata from every supplied matched course candidate; never download books.")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--candidates", type=Path)
    inputs.add_argument("--sitemap", type=Path, help="Saved public OCW global sitemap XML for reproducible candidate discovery.")
    parser.add_argument("--catalog", type=Path, help="Saved MIT course inventory JSON/XZ; required with --sitemap.")
    parser.add_argument("--eecs-mappings", type=Path, help="Saved official EECS renumbering rows; removed subjects are excluded.")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--checked-on", default="2026-10-01")
    parser.add_argument("--reparse", action="store_true", help="Parse cached pages again; preserve cached source responses and access limits.")
    parser.add_argument("--all-archives", action="store_true", help="Inspect every dated OCW source version and preserve unmatched academic identities as historical archive courses.")
    parser.add_argument("--archive-out", type=Path, help="Separate fresh historical course inventory folder; required with --all-archives.")
    parser.add_argument("--previous-evidence", type=Path, help="Prior preserved OCW evidence; omit already linked source records from the new supplement.")
    parser.add_argument("--offline", action="store_true", help="Use cached public metadata only; never fetch missing source pages.")
    args = parser.parse_args(argv)
    if args.out.is_symlink() or (args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir()))):
        raise ValueError("Output must be a new or empty regular folder")
    if args.cache.resolve() == args.out.resolve() or args.out.resolve() in args.cache.resolve().parents:
        raise ValueError("Persistent cache must be separate from the fresh snapshot output")
    if args.all_archives:
        if not args.sitemap or not args.catalog or not args.archive_out or not args.previous_evidence:
            parser.error("--all-archives requires --sitemap, --catalog, --archive-out, and --previous-evidence")
        archive_folder = args.archive_out
        if archive_folder.is_symlink() or (archive_folder.exists() and (not archive_folder.is_dir() or any(archive_folder.iterdir()))):
            raise ValueError("Historical inventory output must be a new or empty regular folder")
        validate_archive_paths(args.out, archive_folder, args.cache)
        inputs = [args.sitemap, args.catalog, args.previous_evidence] + ([args.eecs_mappings] if args.eecs_mappings else [])
        captured_inputs = [path.read_bytes() for path in inputs]
        mappings = json.loads(captured_inputs[3]) if args.eecs_mappings else []
        if isinstance(mappings, dict):
            mappings = mappings["mappings"]
        candidates = archive_candidates(captured_inputs[0])
        catalog_payload = json.loads(lzma.decompress(captured_inputs[1]) if args.catalog.suffix == ".xz" else captured_inputs[1])
        catalog = catalog_payload["courses"] if isinstance(catalog_payload, dict) else catalog_payload
        prior = json.loads(captured_inputs[2])["evidence"]
        client = CachedClient(args.cache, offline=args.offline)
        results_path = args.cache / "archive-results.json"
        evidence, quarantine, courses, outcomes, stopped, completed = harvest_archives(candidates, catalog, mappings, client, results_path, prior, reparse=args.reparse, workers=args.workers, input_hashes=[hashlib.sha256(body).hexdigest() for body in captured_inputs])
        summary = write_archive_snapshot(args.out, archive_folder, candidates, courses, evidence, quarantine, outcomes, stopped, completed, args.checked_on, inputs, client, workers=args.workers, captured_inputs=captured_inputs)
        print(json.dumps(summary, indent=2))
        return 2 if stopped else 0
    if args.sitemap:
        if args.catalog is None:
            parser.error("--sitemap requires --catalog")
        mappings = json.loads(args.eecs_mappings.read_text()) if args.eecs_mappings else None
        if isinstance(mappings, dict):
            mappings = mappings["mappings"]
        candidates = discover_candidates(args.sitemap.read_bytes(), load_catalog(args.catalog), mappings)
    else:
        candidates = json.loads(args.candidates.read_text())
        if isinstance(candidates, dict):
            candidates = list(candidates.values())
    client = CachedClient(args.cache, offline=args.offline)
    results_path = args.cache / "results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() and not args.reparse else {}
    def candidate_key(candidate):
        return candidate["row"]["course_key"] + ":" + hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()[:16]
    wanted = {candidate_key(candidate) for candidate in candidates}
    results = {key: value for key, value in results.items() if key in wanted}
    pending = [candidate for candidate in candidates if candidate_key(candidate) not in results]
    stopped = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(research, candidate, client): candidate for candidate in pending}
        for future in concurrent.futures.as_completed(futures):
            candidate = futures[future]
            try:
                pages, outcomes = future.result()
                results[candidate_key(candidate)] = {"evidence": pages, "outcomes": outcomes}
                save_json(results_path, results)
                print(candidate["row"]["code"], "books", sum(len(page["materials"]) for page in pages), "completed", len(results), "/", len(candidates), flush=True)
            except AccessLimit as error:
                stopped = str(error)
                for remaining in futures:
                    remaining.cancel()
                break
    evidence, quarantined, outcomes = [], [], []
    for key in sorted(results):
        outcomes.extend(results[key]["outcomes"])
        for page in results[key]["evidence"]:
            page["evidence_excerpt"] = " ".join(page["evidence_excerpt"].split()[:25])
            (evidence if page["matches"] else quarantined).append(page)
    args.out.mkdir(parents=True, exist_ok=True)
    snapshot = [{key: value for key, value in candidate.items() if key != "row"} | {"row": {key: candidate["row"][key] for key in ("code", "title", "course_key")}} for candidate in candidates]
    candidate_path = args.out / "candidates.json"
    save_json(candidate_path, snapshot, indent=2)
    payload = {"format": "topclass-reading-evidence-v1", "checked_on": args.checked_on,
               "evidence": evidence, "quarantined_evidence": quarantined}
    evidence_path = args.out / "evidence-f.json"
    save_json(evidence_path, payload, indent=2)
    course_book_pairs = {(match["course_key"], plain(item["title"]), item["isbn"] or "", plain(item["edition"] or "")) for record in evidence for match in record["matches"] for item in record["materials"]}
    artifact_paths = [candidate_path, evidence_path]
    if (args.out / "eecs-renumbering.json").is_file():
        artifact_paths.append(args.out / "eecs-renumbering.json")
    summary = {"checked_on": args.checked_on, "source": "https://ocw.mit.edu/sitemap.xml",
               "candidate_count": len(candidates), "completed_candidates": len(results),
               "linked_course_keys": len({match["course_key"] for record in evidence for match in record["matches"]}),
               "linked_sources": len(evidence), "book_mentions": sum(len(page["materials"]) for page in evidence),
               "distinct_course_title_isbn_edition_candidates": len(course_book_pairs),
               "quarantined_sources": len(quarantined), "source_pages": len(outcomes), "stopped": stopped,
               "scope": "Latest direct-code OCW source plus latest official EECS old-code source per candidate catalog course; syllabus and actual linked reading-list HTML. Historical metadata, not complete current adoptions.",
               "files": [{"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in artifact_paths],
               "outcomes": outcomes}
    save_json(args.out / "manifest.json", summary, indent=2)
    print(json.dumps({key: value for key, value in summary.items() if key != "outcomes"}, indent=2))
    return 2 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
