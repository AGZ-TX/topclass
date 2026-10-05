import copy
import json
import re

from bibliographic_findings import finding_aids
from materials_ledger import canonical_isbn


def consolidate_books(books):
    groups = {}
    for book in books:
        isbn = canonical_isbn(str(book.get('isbn') or ''))
        key = 'isbn:' + isbn if isbn else json.dumps(book, sort_keys=True, ensure_ascii=False)
        group = groups.setdefault(key, [])
        if book not in group:
            group.append(book)
    result = []
    for group in groups.values():
        primary = max(group, key=lambda book: (bool(book.get('authors')), sum(bool(book.get(field)) for field in ('title', 'authors', 'edition', 'source_url'))))
        book = copy.deepcopy(primary)
        isbn = canonical_isbn(str(book.get('isbn') or ''))
        if isbn:
            book['isbn'] = isbn
        if len(group) > 1:
            book['bibliographic_variants'] = copy.deepcopy(group)
            book['source_urls'] = list(dict.fromkeys(row['source_url'] for row in group if row.get('source_url')))
            book['finding_aids'] = []
            for row in group:
                for finding in row.get('finding_aids', []):
                    if finding not in book['finding_aids']:
                        book['finding_aids'].append(copy.deepcopy(finding))
            book['edition_verified'] = all(row.get('edition_verified') is True for row in group)
        result.append(book)
    return result


def display_identity(material, identity):
    result = dict(identity)
    title = material.get("resource_title") or material.get("title")
    if isinstance(title, str) and title.strip():
        result["title"] = title.strip()
    authors = result.get("authors", "")
    if "$$Q" in authors:
        authors = re.sub(r"\s*\[(?:author|editor)\]\s*", " ", authors.split("$$Q", 1)[0], flags=re.I).strip()
    citation = material.get("citation", "")
    if isinstance(citation, str):
        marker = re.match(r"^\[([A-Za-z0-9&]{1,12})\]\s*=\s*", citation)
        if marker:
            authors = re.sub(r"^" + re.escape(marker[1]) + r"\]\s*=\s*", "", authors)
    result["authors"] = authors
    if str(result.get("edition", "")).strip().casefold() in {"na", "n/a", "unknown", "not stated", "not_stated"}:
        result["edition"] = ""
    result['finding_aids'] = finding_aids(material)
    return result
