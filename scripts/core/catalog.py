from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import tempfile

from bibliography_display import consolidate_books, display_identity
from education_selection import grounded_fields, candidate_fields, materials_evidence
from materials_ledger import is_learning_book, resource_identity


FORMAT = 'topclass-agent-catalog-v1'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def field_labels(payload):
    labels = payload.get('discovery_academic_areas', payload.get('academic_areas', []))
    return [dict(id=value['id'], label=value['label']) for value in labels
            if isinstance(value, dict) and value.get('id') and value.get('label')]


def readable_course(node, materials):
    payload = node['payload']
    books = []
    seen = set()
    for status in materials:
        for material in status.get('materials', []):
            if not isinstance(material, dict) or not is_learning_book(material):
                continue
            identity = resource_identity(material)
            if not identity:
                continue
            book = display_identity(material, identity) | {
                'reading_role': material.get('assignment_role') or material.get('assigned_vs_suggested_wording') or 'not stated',
                'source_url': material.get('source_url') or material.get('course_source_url'),
                'edition_verified': material.get('bibliography_verified') is True}
            key = encoded(book)
            if key not in seen:
                seen.add(key)
                books.append(book)
    descriptions = [{'field': field, 'text': text} for field, text in grounded_fields(payload)
                    if field in {'description', 'description_variants'}]
    return {'course_id': node['id'], 'title': node['label'], 'university': payload.get('institution') or payload.get('school'),
            'code': payload.get('code'), 'course_code_aliases': payload.get('course_code_aliases', []),
            'fields': field_labels(payload), 'descriptions': descriptions,
            'books': consolidate_books(books), 'source_urls': payload.get('source_urls', []),
            'limits': 'Saved course and bibliography evidence; not current adoption or learned book contents.'}


def export_catalog(catalog, parent):
    parent = Path(parent)
    if parent.is_symlink():
        raise ValueError('Catalog storage cannot be a symlink')
    parent.mkdir(parents=True, exist_ok=True)
    snapshot = dict(catalog.snapshot)
    snapshot.pop('build_seconds', None)
    snapshot['book_selection_policy'] = 'learning-books-v2'
    snapshot['readable_evidence_policy'] = 'course-aliases-book-findings-isbn-groups-v3'
    key = digest(encoded(snapshot).encode())
    destination = parent / key
    if destination.is_symlink():
        raise ValueError('Catalog storage cannot be a symlink')
    if destination.exists():
        library = CatalogLibrary(destination)
        return key, library.manifest_hash
    with tempfile.TemporaryDirectory(prefix='.catalog-', dir=parent) as staging:
        folder = Path(staging)
        evidence_dir = folder / 'evidence'
        evidence_dir.mkdir()
        fields, universities = Counter(), Counter()
        labels = {}
        count = 0
        with (folder / 'courses.jsonl').open('w', encoding='utf-8') as stream:
            nodes = sorted((node for node in catalog.nodes.values() if node['kind'] == 'course'),
                           key=lambda node: (node['label'].casefold(), node['id']))
            for node in nodes:
                materials = materials_evidence(catalog, node['id'])
                record = readable_course(node, materials)
                if not record['books']:
                    continue
                filename = 'evidence/' + digest(node['id'].encode()) + '.json'
                raw = encoded({'course': node, 'materials': materials}).encode()
                (folder / filename).write_bytes(raw)
                record.update(evidence_file=filename, evidence_sha256=digest(raw))
                stream.write(encoded(record) + '\n')
                fields.update({field['id'] for field in record['fields']})
                labels.update({field['id']: field['label'] for field in record['fields']})
                universities.update([record['university'] or 'Not stated'])
                count += 1
        manifest = {'format': FORMAT, 'snapshot': snapshot, 'course_count': count,
                    'order': 'title, then course ID; no relevance scores or shortlist',
                    'courses_file': 'courses.jsonl', 'courses_sha256': digest((folder / 'courses.jsonl').read_bytes()),
                    'fields': [{'id': key, 'label': labels[key], 'course_count': count} for key, count in sorted(fields.items())],
                    'universities': [{'name': name, 'course_count': count} for name, count in sorted(universities.items())],
                    'scope': 'All saved usable course groups in this snapshot. Not a complete university catalog.',
                    'instructions': 'The AI chooses courses. Browse all rows or apply your own literal filters, then inspect full evidence. No course is recommended by this catalog.'}
        raw = encoded(manifest).encode()
        (folder / 'catalog.json').write_bytes(raw)
        folder.rename(destination)
    return key, digest(raw)


class CatalogLibrary:
    def __init__(self, folder, expected_hash=None):
        self.folder = Path(folder)
        raw = self.path('catalog.json').read_bytes()
        self.manifest_hash = digest(raw)
        if expected_hash and self.manifest_hash != expected_hash:
            raise ValueError('Catalog manifest changed; create a new snapshot')
        self.manifest = json.loads(raw)
        if self.manifest.get('format') != FORMAT:
            raise ValueError('Unknown catalog format')
        raw = self.path('courses.jsonl').read_bytes()
        if digest(raw) != self.manifest['courses_sha256']:
            raise ValueError('Catalog JSONL changed; create a new snapshot')
        self.rows = [json.loads(line) for line in raw.splitlines()]
        self.by_id = {row['course_id']: row for row in self.rows}
        if len(self.rows) != self.manifest['course_count'] or len(self.by_id) != len(self.rows):
            raise ValueError('Catalog rows do not match the manifest')
        self.snapshot = self.manifest['snapshot']

    def path(self, name):
        path = self.folder / name
        if self.folder.is_symlink() or any(part == '..' for part in Path(name).parts):
            raise ValueError('Invalid catalog path')
        if not path.resolve().is_relative_to(self.folder.resolve()) or path.is_symlink():
            raise ValueError('Catalog path leaves the snapshot')
        return path

    def evidence(self, course_id):
        if course_id not in self.by_id:
            raise ValueError('Unknown course ID in this catalog')
        row = self.by_id[course_id]
        raw = self.path(row['evidence_file']).read_bytes()
        if digest(raw) != row['evidence_sha256']:
            raise ValueError('Course evidence changed; create a new snapshot')
        evidence = json.loads(raw)
        if evidence['course']['id'] != course_id:
            raise ValueError('Course evidence identity mismatch')
        return evidence

    def get(self, course_id):
        return self.evidence(course_id)['course']

    def materials_evidence(self, course_id):
        return self.evidence(course_id)['materials']

    def browse(self, field=None, university=None, contains=None, offset=0, limit=25):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or not 1 <= limit <= 100:
            raise ValueError('Use a nonnegative offset and page size from 1 to 100')
        rows = [row for row in self.rows
                if (not field or field.casefold() in {value[key].casefold() for value in row['fields'] for key in ('id', 'label')})
                and (not university or university.casefold() == str(row['university']).casefold())
                and (not contains or contains.casefold() in encoded(row).casefold())]
        page = rows[offset:offset + limit]
        return {'total': len(rows), 'offset': offset, 'next_offset': offset + len(page) if offset + len(page) < len(rows) else None,
                'order': self.manifest['order'], 'filters': {'field': field, 'university': university, 'contains': contains},
                'courses': page}

    def inspect(self, course_id):
        evidence = self.evidence(course_id)
        node = evidence['course']
        return {'course_id': course_id, 'title': node['label'], 'course': node,
                'fields': [{'field': field, 'text': text} for field, text in candidate_fields(self, node)],
                'materials': evidence['materials'], 'book_knowledge_processed': False, 'untrusted_evidence': True}
