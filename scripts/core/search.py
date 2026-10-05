from core.system import lock_file
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from bibliographic_findings import valid_isbn


REPOSITORY = 'https://github.com/AGZ-TX/search'
UNCONFIRMED = {'library-title-only-candidate', 'library-work-only-candidate'}


def write_json(path, value):
    with path.open('x', encoding='utf-8') as handle:
        path.chmod(0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def replace_json(path, value):
    if path.is_symlink():
        raise ValueError('Search state cannot use symlinks')
    descriptor, name = tempfile.mkstemp(prefix='.search-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def search_request(plan, agent_id, selection_id):
    books, queries = [], []
    for index, material in enumerate(plan.get('materials_to_supply', [])):
        identity = material.get('display_identity') or material['identity_candidate']
        evidence = []
        assigned = valid_isbn(material['identity_candidate'].get('isbn'))
        if assigned:
            evidence.append({'isbn': assigned, 'basis': 'original-course-book-identity',
                             'edition_status': material['identity_candidate'].get('certainty', ''),
                             'title': identity['title'], 'edition': identity.get('edition', '')})
        for finding in identity.get('finding_aids', []):
            isbn = valid_isbn(finding.get('isbn'))
            if isbn and finding.get('basis') and finding['basis'] not in UNCONFIRMED:
                evidence.append(dict(finding, isbn=isbn))
        isbns = list(dict.fromkeys(item['isbn'] for item in evidence))
        for isbn in isbns:
            if isbn not in queries:
                queries.append(isbn)
        assignments = material.get('assignments', [])
        sources = list(dict.fromkeys(url for assignment in assignments
            for url in [assignment.get('evidence', {}).get('source_url'),
                        assignment.get('evidence', {}).get('course_source_url'),
                        *(source.get('source_url') for source in assignment.get('sources', []))]
            if isinstance(url, str) and url.startswith(('https://', 'http://'))))
        for finding in identity.get('finding_aids', []):
            url = finding.get('source_url')
            if isinstance(url, str) and url.startswith(('https://', 'http://')) and url not in sources:
                sources.append(url)
        books.append({'book_index': index, 'title': identity['title'], 'authors': identity.get('authors', ''),
                      'edition': identity.get('edition', ''), 'isbns': isbns, 'isbn_evidence': evidence,
                      'course_ids': list(dict.fromkeys(row['course_id'] for row in assignments)),
                      'sources': sources, 'status': 'ready' if isbns else 'no-supported-isbn'})
    return {'format': 'topclass-book-search-request-v1', 'agent_id': agent_id, 'selection_id': selection_id,
            'plan_fingerprint': plan['user_selection']['plan_fingerprint'],
            'snapshot_fingerprint': plan['user_selection']['snapshot_fingerprint'],
            'repository': REPOSITORY, 'isbns': queries, 'books': books}


def validate_repository(value):
    root = Path(value).expanduser().absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError('Use a search checkout without symlinked directories')
    for name in ('search.py', 'dist/index.js', 'package.json'):
        path = root / name
        if not path.is_file() or any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError('Prepare the AGZ-TX/search checkout with npm ci --ignore-scripts and npm run build')
    package = json.loads((root / 'package.json').read_text(encoding='utf-8'))
    if not isinstance(package, dict):
        raise ValueError('Search package metadata must be an object')
    repository = package.get('repository', {})
    url = repository.get('url', '') if isinstance(repository, dict) else repository
    if not isinstance(url, str) or url.removeprefix('git+').removesuffix('.git').rstrip('/') != REPOSITORY:
        raise ValueError('Use the AGZ-TX/search checkout, not the upstream npm package')
    return root


def configured_repository(home, project_root, explicit=None):
    if explicit:
        return validate_repository(explicit)
    if value := os.environ.get('TOPCLASS_SEARCH_REPO'):
        return validate_repository(value)
    config = home / '.search.json'
    if config.is_symlink():
        raise ValueError('Search configuration cannot be a symlink')
    if config.exists():
        payload = json.loads(config.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('format') != 'topclass-book-search-config-v1':
            raise ValueError('Invalid search configuration')
        if not isinstance(payload.get('repository_path'), str) or not payload['repository_path']:
            raise ValueError('Search configuration needs a checkout path')
        return validate_repository(payload['repository_path'])
    sibling = project_root.parent / 'search'
    if sibling.exists():
        return validate_repository(sibling)
    bundled = project_root / 'scripts' / 'finder'
    return validate_repository(bundled) if (bundled / 'dist' / 'index.js').is_file() else None


def accepted_file(record, state_dir):
    if not isinstance(record, dict):
        raise ValueError('Search file results must be objects')
    if record.get('status') != 'file_verified' or record.get('isbn_match') != 'verified':
        return None
    value = record.get('path')
    if not isinstance(value, str):
        raise ValueError('Checked search result has no local file')
    path = Path(value)
    allowed = state_dir / 'checked'
    if not path.is_absolute() or not path.resolve().is_relative_to(allowed.resolve()):
        raise ValueError('Search returned a file outside this selection')
    if not path.is_file() or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Checked search files cannot use symlinks')
    if record.get('format') not in {'pdf', 'epub', 'mobi', 'djvu'}:
        raise ValueError('Search returned an unsupported document format')
    sha256, md5, size = hashlib.sha256(), hashlib.md5(), 0
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            sha256.update(block)
            md5.update(block)
            size += len(block)
    if not size or sha256.hexdigest() != record.get('sha256') or md5.hexdigest() != record.get('md5') or size != record.get('bytes'):
        raise ValueError('Checked search file changed or has inconsistent hashes')
    checks = record.get('checks')
    if (not isinstance(checks, list) or any(not isinstance(check, str) for check in checks)
            or not {'catalog-md5', 'supported-format', 'sha256-stability'}.issubset(checks)
            or not isinstance(record.get('checked_at'), str) or not record['checked_at'].strip()):
        raise ValueError('Search result is missing file-check provenance')
    return record | {'path': str(path)}


def import_report(report_path, isbns, state_dir):
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if not isinstance(report, dict) or report.get('isbns') != isbns or not isinstance(report.get('results'), list):
        raise ValueError('Search report does not match the requested ISBN batch')
    rows, files = [], []
    for row in report['results']:
        if not isinstance(row, dict) or not isinstance(row.get('results', []), list):
            raise ValueError('Invalid ISBN result in search report')
        isbn = row.get('isbn')
        if isbn not in isbns or isbn in [item['isbn'] for item in rows]:
            raise ValueError('Search report contains unexpected or duplicate ISBNs')
        accepted = []
        for record in row.get('results', []):
            if file := accepted_file(record, state_dir):
                accepted.append(file)
                files.append(file | {'isbn': isbn})
        status = 'file_verified' if accepted else row.get('status', 'no_verified_result')
        if status == 'file_verified' and not accepted:
            raise ValueError('Search claimed success without a checked file')
        rows.append({'isbn': isbn, 'status': status, 'files': accepted,
                     'source_errors': row.get('source_errors', []), 'reason': row.get('reason', '')})
    for isbn in isbns:
        if isbn not in [item['isbn'] for item in rows]:
            rows.append({'isbn': isbn, 'status': 'not-completed', 'files': []})
    return rows, files


def save_summary(pointer, summary):
    replace_json(pointer, summary)
    if summary.get('directory'):
        replace_json(Path(summary['directory']) / 'summary.json', summary)


def run_search(selection_dir, request, repository):
    pointer = selection_dir / 'book-search.json'
    summary = {'format': 'topclass-book-search-result-v1', 'agent_id': request['agent_id'],
               'selection_id': request['selection_id'], 'requested_isbns': request['isbns'],
               'unresolved_books': [book for book in request['books'] if not book['isbns']],
               'results': [], 'files': [], 'reports': [], 'status': 'pending', 'learned': False}
    if not request['isbns']:
        summary['status'] = 'not-needed'
        save_summary(pointer, summary)
        return summary
    if repository is None:
        summary.update(status='setup-required', reason='Connect a prepared AGZ-TX/search checkout to find these books.')
        save_summary(pointer, summary)
        return summary
    runs = selection_dir / 'book-search'
    if runs.is_symlink():
        raise ValueError('Search runs cannot use symlinks')
    runs.mkdir(mode=0o700, exist_ok=True)
    directory = runs / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    summary.update(status='running', directory=str(directory), repository_path=str(repository))
    save_summary(pointer, summary)
    lock = selection_dir.parent.parent / '.book-search.lock'
    if lock.is_symlink():
        raise ValueError('Search locks cannot use symlinks')
    with lock.open('a') as handle:
        lock.chmod(0o600)
        try:
            lock_file(handle, blocking=False)
        except BlockingIOError:
            summary.update(status='busy', reason='Another book search is running for this agent; retry after it finishes.')
            save_summary(pointer, summary)
            return summary
        try:
            for offset in range(0, len(request['isbns']), 100):
                values = request['isbns'][offset:offset + 100]
                batch = str(offset // 100 + 1).zfill(4)
                inputs, report = directory / ('isbns-' + batch + '.json'), directory / ('report-' + batch + '.json')
                state, log = directory / ('files-' + batch), directory / ('search-' + batch + '.log')
                write_json(inputs, values)
                receipt = {'input': str(inputs), 'report': str(report), 'log': str(log), 'exit_code': None}
                summary['reports'].append(receipt)
                save_summary(pointer, summary)
                with log.open('x') as output:
                    log.chmod(0o600)
                    process = subprocess.run([sys.executable, str(repository / 'search.py'), '--input', str(inputs),
                                              '--state-dir', str(state), '--output', str(report)],
                                             cwd=repository, stdin=subprocess.DEVNULL, stdout=output, stderr=output)
                receipt['exit_code'] = process.returncode
                has_report = report.is_file() and not report.is_symlink() and report.stat().st_size > 0
                if has_report:
                    rows, files = import_report(report, values, state)
                    summary['results'].extend(rows)
                    summary['files'].extend(files)
                if process.returncode == 130:
                    summary.update(status='interrupted', reason='Search stopped; completed checked files and reports are retained.')
                    break
                if process.returncode not in (0, 2):
                    summary.update(status='failed' if has_report else 'setup-required',
                                   reason='Search could not complete; check its private log.')
                    break
                if not has_report:
                    raise ValueError('Search completed without an ISBN results report')
                save_summary(pointer, summary)
            else:
                found = {row['isbn'] for row in summary['results'] if row['files']}
                summary['status'] = 'complete' if found == set(request['isbns']) and not summary['unresolved_books'] else 'partial'
        except KeyboardInterrupt:
            summary.update(status='interrupted', reason='Search stopped; prior reports and checked files are retained.')
            save_summary(pointer, summary)
            raise
        except (OSError, ValueError, TypeError, KeyError) as error:
            summary.update(status='failed', reason=str(error))
        save_summary(pointer, summary)
    return summary
