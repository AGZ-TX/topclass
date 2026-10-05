import copy
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlparse

try:
    from .book_matches import book_isbn, edition_identity
    from .public_subject_evidence import identity_dates, identity_matches, public_url
except ImportError:
    from book_matches import book_isbn, edition_identity
    from public_subject_evidence import identity_dates, identity_matches, public_url


ROOT = Path(__file__).resolve().parents[1]
FOLDER = "bibliographic-subject-evidence-2026-10-01"
BLANK_ISBN_CONTEXT = {
    "9781787142237": {"url": "https://bookstore.emerald.com/how-mediation-works-pb-9781787142237.html", "year": "2017", "publisher": "Emerald", "catalog_publisher": "Emerald Publishing Limited",
                      "keys": {"stanford:public-archive:3c51a4892ea492fac3cb"}},
    "9780593511695": {"url": "https://penguinrandomhousehighereducation.com/book/?isbn=9780593511695", "year": "2023", "publisher": "Penguin", "catalog_publisher": "Penguin Books",
                      "keys": {"stanford:public-archive:32056630b90c8475bdb3"}},
    "9780143118442": {"url": "https://catalog.wake.gov/Record/277080", "year": "2010", "publisher": "Penguin", "catalog_publisher": "Penguin Books",
                      "keys": {"stanford:public-archive:55813c60c4825cb312a8", "stanford:public-archive:783a76f084a8ec078fba"}},
}
GENRE = re.compile(r"\b(?:fiction|novels?|poetry|poetic works|drama|literary criticism|sea stories|romans et récits jeunesse)\b", re.I)
SUBJECTS = {
    "humanities-history-languages": re.compile(r"\b(?:philology|history|historical writing|language|grammar|rhetoric|writing challenges|archaeology|memoirs?|autobiographies|fairy tales|folklore)\b", re.I),
    "natural-physical-sciences": re.compile(r"\b(?:botany|physics|chemistry|biology|astronomy|geology)\b", re.I),
    "mathematics-statistics": re.compile(r"\b(?:mathematics|statistics|statistical analysis)\b", re.I),
    "social-sciences": re.compile(r"\b(?:sociology|anthropology|ethnology|political science|social sciences?|radical politics|domestic politics|world politics)\b", re.I),
    "psychology-cognitive-science": re.compile(r"\bpsychology\b", re.I),
    "arts-design-media": re.compile(r"\b(?:art|modern art|renaissance art|painting|design principles)\b", re.I),
    "education-learning": re.compile(r"\beducation\b", re.I),
    "law-policy-governance": re.compile(r"\b(?:political science|law|domestic politics|world politics)\b", re.I),
    "philosophy-religion-ethics": re.compile(r"\b(?:philosophy|ethics|religion|theology)\b", re.I),
    "communication-journalism": re.compile(r"\b(?:written communication|speech communication|interpersonal communication|journalism)\b", re.I),
    "business-economics-management": re.compile(r"\b(?:economics|business management|industrial management|organizational behavior|capitalism)\b", re.I),
}
REVIEWED_PUBLISHERS = {
    "9781319413019": "https://www.macmillanlearning.com/college/us/product/A-Pocket-Style-Manual/p/1319413013",
    "9782070650644": "https://www.cercle-enseignement.com/Ouvrages/Gallimard-Jeunesse/Folio-Junior-Textes-classiques/Vendredi-ou-la-vie-sauvage",
    "9780691182223": "https://press.princeton.edu/books/hardcover/9780691182223/how-logic-works",
    "9781570273681": "https://www.minorcompositions.info/?p=991",
    "9780872205581": "https://newsouthbooks.com.au/books/the-annals/",
    "9781583225813": "https://penguinrandomhouselibrary.com/book/?isbn=9781583225813",
    **{key: context["url"] for key, context in BLANK_ISBN_CONTEXT.items() if key != "9780143118442"},
}
POLICY_VERSION = "bibliographic-subject-context-v2-" + hashlib.sha256(json.dumps({
    "subjects": {key: {"pattern": value.pattern, "flags": value.flags} for key, value in SUBJECTS.items()},
    "genre": {"pattern": GENRE.pattern, "flags": GENRE.flags},
    "blank_isbn_context": {key: {**value, "keys": sorted(value["keys"])} for key, value in BLANK_ISBN_CONTEXT.items()},
    "reviewed_publishers": REVIEWED_PUBLISHERS,
}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def string_list(value):
    return value if isinstance(value, list) else [value] if isinstance(value, str) and value else []


def title_words(value):
    text = normalized(value).split(" — ")[0].split(" / ")[0].split(":")[0]
    return tuple(word for word in re.findall(r"[\w]+", text) if word not in {"a", "an", "the"})


def contributor_match(assigned, returned):
    for author in assigned:
        tokens = re.findall(r"\w+", normalized(author))
        if not tokens:
            continue
        for contributor in returned:
            other = re.findall(r"\w+", normalized(contributor))
            if tokens == other:
                return True
            if "," in author:
                family = re.findall(r"\w+", normalized(author.split(",")[0]))
                given = re.findall(r"\w+", normalized(author.split(",", 1)[1]))
            else:
                family, given = tokens[-1:], tokens[:-1]
            if not given or len(other) <= len(family) or other[-len(family):] != family:
                continue
            other_given = other[:-len(family)]
            if len(given) == len(other_given) and all(left == right or (len(left) == 1 and right.startswith(left)) or (len(right) == 1 and left.startswith(right)) for left, right in zip(given, other_given)):
                return True
    return False


def catalog_identity_matches(reading, catalog):
    original, returned = title_words(reading["title"]), title_words(catalog["title"])
    if original == returned and original and contributor_match(reading["authors"], catalog["authors"]):
        return True
    return any(alias.get("assigned_title") == reading["title"] and alias.get("assigned_authors") == reading["authors"]
               and alias.get("catalog_title") == catalog["title"] and alias.get("catalog_authors") == catalog["authors"]
               and alias.get("review_method") == "bounded-original-pilot-title-contributor-comparison"
               for alias in catalog.get("identity_aliases", []))


def reading_binding(reading, course_key):
    declared = reading.get("course_key", course_key)
    if declared != course_key:
        raise ValueError("Bibliographic reading belongs to a different source course")
    authors = reading.get("authors", [])
    authors = sorted({normalized(name) for value in string_list(authors) for name in value.split(";") if name.strip()})
    editions = reading.get("assigned_editions", reading.get("assigned_edition", reading.get("edition", [])))
    isbns = reading.get("assigned_isbns", reading.get("assigned_isbn", reading.get("isbn", [])))
    urls = reading.get("source_urls", [])
    urls = sorted(set(string_list(urls) + string_list(reading.get("course_source_url")) + string_list(reading.get("source_url"))))
    return {"course_key": course_key, "title": normalized(reading.get("title") or reading.get("book_title") or reading.get("resource_title") or ""),
            "authors": authors, "assigned_editions": sorted({edition_identity(value) for value in string_list(editions) if value}),
            "assigned_isbns": sorted({book_isbn(value) for value in string_list(isbns) if book_isbn(value)}),
            "citation": reading.get("citation", reading.get("course_citation", "")), "source_urls": urls,
            "source_year": reading.get("source_year", ""), "source_course_versions": reading.get("source_course_versions", []),
            "book_id": reading.get("book_id", ""), "covered_group_id": reading.get("covered_group_id", "")}


def catalog_subjects(catalog, taxonomy):
    allowed = {area["id"]: area["label"] for area in taxonomy["academic_areas"]}
    subjects = catalog.get("subjects", [])
    if not isinstance(subjects, list) or any(not isinstance(value, str) or not value.strip() for value in subjects):
        raise ValueError("Bibliographic subjects must be literal public subject strings")
    literary = any(literary_subject(value) for value in subjects)
    fiction_genre = any(GENRE.search(value) and not re.search(r"\bnon[ -]?fiction\b", value, re.I) for value in subjects)
    result = []
    for identifier, pattern in SUBJECTS.items():
        if fiction_genre and identifier != "humanities-history-languages":
            continue
        anchors = [value for value in subjects if pattern.search(value) or (identifier == "humanities-history-languages" and literary_subject(value))]
        if anchors and identifier in allowed:
            result.append({"id": identifier, "label": allowed[identifier], "subjects": anchors})
    return result


def literary_subject(value):
    if re.search(r"\bnon[ -]?fiction\b", value, re.I):
        return False
    if GENRE.search(value):
        return True
    return bool(re.search(r"\bliterature\b", value, re.I) and not re.search(r"\b(?:scientific|medical|technical|research|chemistry|biology|physics)\b", value, re.I))


def validate_supplement(supplement, taxonomy):
    if supplement.get("format") != "topclass-bibliographic-subject-evidence-v1" or supplement.get("policy_version") != POLICY_VERSION:
        raise ValueError("Unknown bibliographic evidence format or policy")
    catalogs = supplement.get("catalogs", {})
    for isbn, catalog in catalogs.items():
        if not book_isbn(isbn) or book_isbn(isbn) != isbn or isbn not in {book_isbn(value) for value in catalog.get("isbns", [])}:
            raise ValueError("Bibliographic catalog must explicitly include its valid assigned ISBN")
        expected_review = "title-contributor-year-bound-candidate" if isbn in BLANK_ISBN_CONTEXT else "title-author-compatible-assigned-isbn-bound-candidate"
        if not public_url(catalog.get("source_url")) or catalog.get("identity_review") != expected_review:
            raise ValueError("Bibliographic catalog identity or public URL is not accepted")
        if not isinstance(catalog.get("title"), str) or not catalog["title"] or not catalog.get("authors"):
            raise ValueError("Bibliographic title and contributor metadata are required")
        receipt = catalog.get("receipt")
        if not isinstance(receipt, dict) or catalog.get("receipt_sha256") != fingerprint(receipt) or not public_url(catalog.get("receipt_url")) or not isinstance(catalog.get("retrieved_at"), str) or not re.match(r"^\d{4}-\d{2}-\d{2}", catalog["retrieved_at"]):
            raise ValueError("Bibliographic receipt, snapshot hash, URL or retrieval date is invalid")
        if catalog.get("receipt_kind") == "openlibrary-books-api":
            url = urlparse(catalog["receipt_url"])
            query = parse_qs(url.query)
            if url.hostname != "openlibrary.org" or url.path != "/api/books" or query.get("jscmd") != ["data"] or "ISBN:" + isbn not in ",".join(query.get("bibkeys", [])).split(",") or urlparse(catalog["source_url"]).hostname != "openlibrary.org":
                raise ValueError("Bibliographic API receipt does not bind to the assigned ISBN")
            expected_subjects = [subject["name"] for subject in receipt.get("subjects", [])]
            expected_authors = [author["name"] for author in receipt.get("authors", [])]
            expected_isbns = [value for field in ("isbn_10", "isbn_13") for value in receipt.get("identifiers", {}).get(field, [])]
        elif catalog.get("receipt_kind") == "official-publisher-public-metadata":
            if catalog["source_url"] != REVIEWED_PUBLISHERS.get(isbn) or catalog["receipt_url"] != catalog["source_url"]:
                raise ValueError("Publisher subject receipt is outside the reviewed public pilot")
            expected_subjects, expected_authors, expected_isbns = receipt.get("subjects"), receipt.get("authors"), receipt.get("isbns")
        elif catalog.get("receipt_kind") == "government-public-catalog-metadata":
            if isbn != "9780143118442" or catalog["source_url"] != BLANK_ISBN_CONTEXT[isbn]["url"] or catalog["receipt_url"] != catalog["source_url"]:
                raise ValueError("Government catalog receipt is outside the reviewed exact public record")
            expected_subjects, expected_authors, expected_isbns = receipt.get("subjects"), receipt.get("authors"), receipt.get("isbns")
        else:
            raise ValueError("Unknown public bibliographic receipt kind")
        if catalog.get("subjects") != expected_subjects or catalog.get("authors") != expected_authors or catalog.get("isbns") != expected_isbns or catalog.get("title") != receipt.get("title") or catalog.get("source_url") != receipt.get("url"):
            raise ValueError("Bibliographic fields do not match the original receipt")
        catalog_subjects(catalog, taxonomy)
    seen = set()
    for binding in supplement.get("bindings", []):
        isbn = binding.get("catalog_isbn", binding.get("assigned_isbn"))
        reading = binding.get("reading", {})
        identity = binding.get("source_identity", {})
        key = binding.get("course_key")
        blank_context = binding.get("identity_mode") == "publisher-title-contributor-year-context"
        if blank_context:
            policy = BLANK_ISBN_CONTEXT.get(isbn)
            catalog = catalogs.get(isbn, {})
            year, publisher = binding.get("publication_year"), binding.get("publisher")
            if catalog.get("receipt", {}).get("publish_date") != year:
                raise ValueError("Blank ISBN publication year does not match the original public receipt")
            if not policy or key not in policy["keys"] or binding.get("assigned_isbn") != "" or reading.get("assigned_isbns") != [] or year != policy["year"] or publisher != policy["publisher"] or catalog.get("publish_date") != year or catalog.get("source_url") != policy["url"] or catalog.get("receipt", {}).get("publisher") != policy["catalog_publisher"] or not re.search(r"\b" + year + r"\b", reading.get("citation", "")) or publisher.casefold() not in reading.get("citation", "").casefold():
                raise ValueError("Blank assigned ISBN requires frozen exact publisher/year and source-course context")
        elif binding.get("identity_mode") not in (None, "assigned-isbn") or "catalog_isbn" in binding or isbn not in reading.get("assigned_isbns", []):
            raise ValueError("Assigned ISBN binding cannot use a bibliographic candidate ISBN")
        if isbn not in catalogs or reading.get("course_key") != key or not reading.get("title") or not reading.get("authors") or not reading.get("citation") or not reading.get("source_urls"):
            raise ValueError("Bibliographic supplement requires exact original assigned reading metadata")
        if any(not public_url(url) for url in reading["source_urls"]) or not identity.get("title") or not identity.get("code") or set(identity.get("dates", {})) != set(identity_dates({})):
            raise ValueError("Bibliographic source identity, dates or citation URL is invalid")
        if not catalog_identity_matches(reading, catalogs[isbn]):
            raise ValueError("Assigned title or contributor conflicts with public bibliographic identity")
        identifier = (key, fingerprint(reading), isbn)
        if identifier in seen:
            raise ValueError("Duplicate bibliographic assignment binding")
        seen.add(identifier)
    return supplement


def load_supplement(taxonomy, root=ROOT):
    folder = Path(root) / "data/expansion" / FOLDER
    manifest = json.loads((folder / "manifest.json").read_text())
    raw = (folder / "evidence.json").read_bytes()
    if manifest.get("format") != "topclass-bibliographic-subject-evidence-v1" or manifest.get("policy_version") != POLICY_VERSION or manifest.get("evidence_sha256") != hashlib.sha256(raw).hexdigest():
        raise ValueError("Bibliographic evidence snapshot hash changed")
    supplement = validate_supplement(json.loads(raw), taxonomy)
    if manifest.get("catalogs") != len(supplement["catalogs"]) or manifest.get("bindings") != len(supplement["bindings"]):
        raise ValueError("Bibliographic evidence manifest counts changed")
    return supplement


def apply_bibliographic_subjects(course, taxonomy, readings, supplement=None):
    supplement = load_supplement(taxonomy) if supplement is None else validate_supplement(supplement, taxonomy)
    result = copy.deepcopy(course)
    key = course.get("course_key")
    current = {fingerprint(reading_binding(reading, key)) for reading in readings}
    matches = [binding for binding in supplement["bindings"] if binding["course_key"] == key
               and identity_matches(course, binding["source_identity"]) and fingerprint(binding["reading"]) in current]
    if not matches:
        return result
    facets = {facet["id"]: facet for facet in result.get("discovery_academic_areas", [])}
    provenance = []
    for binding in matches:
        candidate = binding.get("catalog_isbn")
        catalog = supplement["catalogs"][candidate or binding["assigned_isbn"]]
        for area in catalog_subjects(catalog, taxonomy):
            anchors = [{"kind": "reading", "field": "bibliographic_subject", "quote": subject,
                        "course_key": key, "source_urls": binding["reading"]["source_urls"],
                        "bibliographic_source_url": catalog["source_url"], "assigned_isbn": binding["assigned_isbn"],
                        "course_citation": binding["reading"]["citation"], "source_year": binding["reading"]["source_year"]}
                       for subject in area["subjects"]]
            if candidate:
                for anchor in anchors:
                    anchor["bibliographic_candidate_isbn"] = candidate
            context_basis = "catalog-title-contributor-year-subject-candidate" if catalog["receipt_kind"] == "government-public-catalog-metadata" else "publisher-title-contributor-year-subject-candidate"
            facet = {"id": area["id"], "label": area["label"], "basis": context_basis if candidate else "assigned-reading-bibliographic-subject-candidate",
                     "anchor_candidates": anchors, "source_fingerprint": fingerprint(binding),
                     "human_reviewed": False, "current_adoption_verified": False, "task_fit_verified": False,
                     "catalog_metadata_review": "unreviewed-subject-semantics", "edition_status": catalog["edition_status"]}
            if candidate:
                facet["source_status"] = catalog["receipt_kind"]
            provenance.append(facet)
            facets.setdefault(area["id"], facet)
    if provenance:
        result["discovery_academic_areas"] = [facets[identifier] for identifier in sorted(facets)]
        result["bibliographic_subject_evidence"] = {"policy_version": POLICY_VERSION, "facets": provenance,
            "human_reviewed": False, "current_adoption_verified": False, "task_fit_verified": False,
            "limit": "Public assigned-book subject context, not original course outcomes or learned knowledge. Catalog subjects may be aggregated or erroneous."}
    return result
