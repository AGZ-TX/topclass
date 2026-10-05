from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .materials_ledger import canonical_isbn, material_title
except ImportError:
    from materials_ledger import canonical_isbn, material_title


PATH = Path(__file__).resolve().parents[1] / 'data/book-evidence/isbn-findings.json'
FORMAT = 'topclass-book-finding-evidence-v1'


def valid_isbn(value):
    if not isinstance(value, str):
        return ''
    try:
        result = canonical_isbn(value)
    except ValueError:
        return ''
    return result if result.startswith(('978', '979')) else ''


def material_identity(material):
    return {'title': material_title(material), 'authors': material.get('authors') or material.get('author') or '',
            'edition': material.get('edition') or '', 'citation': material.get('citation') or '',
            'source_url': material.get('source_url') or material.get('course_source_url') or ''}


def identity_key(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def source_url(value):
    if not isinstance(value, str):
        return ''
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {'https', 'http'} and parsed.netloc else ''
    except ValueError:
        return ''


def saved_findings(material):
    identity = material_identity(material)
    bibliography = material.get('library_bibliography')
    if not isinstance(bibliography, dict):
        try:
            bibliography = json.loads(material.get('citation') or '{}')
        except (ValueError, TypeError):
            bibliography = {}
    if not isinstance(bibliography, dict):
        return []
    titles = bibliography.get('title', [])
    if isinstance(titles, list) and titles and not any(
            isinstance(title, str) and ' '.join(title.casefold().split()).strip(' .:/') ==
            ' '.join(identity['title'].casefold().split()).strip(' .:/') for title in titles):
        return []
    values = bibliography.get('isbn', [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    values = list(values)
    identifiers = bibliography.get('identifier', [])
    if isinstance(identifiers, list):
        for identifier in identifiers:
            if isinstance(identifier, str):
                values.extend(re.findall(r'\$\$CISBN\$\$V([0-9Xx -]+)', identifier))
    isbns = list(dict.fromkeys(isbn for value in values if (isbn := valid_isbn(value))))
    url = source_url(identity['source_url'])
    if not url:
        return []
    return [{'isbn': isbn, 'title': identity['title'], 'authors': identity['authors'], 'edition': identity['edition'],
             'source_url': url, 'basis': 'saved-library-record', 'edition_status': 'library edition; course assignment unconfirmed'}
            for isbn in isbns]


@lru_cache(maxsize=4)
def read_findings(path, modified, size):
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if payload.get('format') != FORMAT or not isinstance(payload.get('records'), dict):
        raise ValueError('Invalid book finding evidence')
    records = payload['records']
    for key, record in records.items():
        if identity_key(record['source_identity']) != key:
            raise ValueError('Book finding evidence identity mismatch')
        if not isinstance(record.get('findings'), list):
            raise ValueError('Book finding evidence needs a findings list')
        for finding in record['findings']:
            if not source_url(finding.get('source_url')) or not finding.get('title') or not finding.get('basis'):
                raise ValueError('Book finding needs title, source and basis')
            if finding.get('isbn') and valid_isbn(finding['isbn']) != finding['isbn']:
                raise ValueError('Book finding ISBN must be valid ISBN-13')
            if not finding.get('edition_status'):
                raise ValueError('Book finding must preserve edition uncertainty')
    return records


def finding_aids(material, path=PATH):
    findings = saved_findings(material)
    path = Path(path)
    if path.exists():
        stat = path.stat()
        record = read_findings(str(path), stat.st_mtime_ns, stat.st_size).get(identity_key(material_identity(material)))
        if record:
            findings.extend({key: finding[key] for key in ('isbn', 'title', 'authors', 'edition', 'source_url', 'basis', 'edition_status')
                             if key in finding} for finding in record['findings'])
    seen = set()
    result = []
    for finding in findings:
        key = json.dumps(finding, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result
