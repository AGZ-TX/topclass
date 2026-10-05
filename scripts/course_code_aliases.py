import argparse
import copy
import hashlib
import json
import re
import urllib.request
from datetime import date
from pathlib import Path

REGISTRAR = 'https://registrar.yale.edu/curriculum/course-number-equivalencies'
RESOURCE = '044050bf-386d-4a2e-b578-2a901b85a040'
PUBLIC_API = 'https://wabi-us-east2-b-primary-api.analysis.windows.net'
PUBLIC_REPORT = 'https://app.powerbi.com/view?r=eyJrIjoiMDQ0MDUwYmYtMzg2ZC00YTJlLWI1NzgtMmE5MDFiODVhMDQwIiwidCI6ImRkOGNiZWJiLTIxMzktNGRmOC1iNDExLTRlM2U4N2FiZWI1YyIsImMiOjF9'
COLUMNS = ['College', 'Old Subj. Code', 'Old Course #', 'New Subj Code', 'New Course #', 'Course Title']


def decode_rows(response):
    data = response['results'][0]['result']['data']
    columns = [entry['GroupKeys'][0]['Source']['Property'] for entry in data['descriptor']['Select'][:6]]
    if columns != COLUMNS:
        raise ValueError('Registrar table column meanings changed')
    dataset = data['dsr']['DS'][0]
    rows = next(phase['DM1'] for phase in dataset['PH'] if 'DM1' in phase)
    schema, previous, result = None, None, []
    for row in rows:
        schema = row.get('S', schema)
        values, cells = [], iter(row.get('C', []))
        for index, column in enumerate(schema):
            if row.get('R', 0) & (1 << index):
                if previous is None:
                    raise ValueError('Registrar repeated cell has no preceding row')
                value = previous[index]
            elif row.get('N', 0) & (1 << index):
                value = None
            else:
                value = next(cells)
                if 'DN' in column:
                    value = dataset['ValueDicts'][column['DN']][value]
            values.append(value)
        previous = values
        result.append(dict(zip(COLUMNS, values[:6])))
    return result


def validate_aliases(payload):
    if payload.get('format') != 'topclass-course-code-aliases-v1':
        raise ValueError('Unknown course code alias format')
    source = payload['source']
    date.fromisoformat(source['fetched_on'])
    if source['source_url'] != REGISTRAR or source['embedded_report_url'] != PUBLIC_REPORT:
        raise ValueError('Course aliases lack reviewed Registrar referral')
    if not re.fullmatch('[a-f0-9]{64}', source.get('registrar_document_sha256', '')):
        raise ValueError('Registrar referral hash missing')
    raw = source['response_json']
    if hashlib.sha256(raw.encode()).hexdigest() != source['source_document_sha256']:
        raise ValueError('Registrar mapping response hash mismatch')
    rows = decode_rows(json.loads(raw))
    result = {}
    for record in payload['records']:
        binding = record['identity']
        key = binding['course_key']
        if key not in {'topclass-v0.4:C224', 'topclass-v0.4:C232'} or key in result:
            raise ValueError('Unreviewed or duplicate course alias identity')
        old, new = record['old_code'], record['new_code']
        matches = [row for row in rows if row['College'] == 'YC' and
                row['Old Subj. Code'] + ' ' + row['Old Course #'] == old and
                row['New Subj Code'] + ' ' + row['New Course #'] == new]
        if binding['code'] != new or len(matches) != 1 or record.get('registrar_row') != matches[0]:
            raise ValueError('Course alias lacks a literal Registrar equivalence row')
        if any(not isinstance(binding.get(field), str) or not binding[field].strip() for field in (
                'title', 'source_url', 'version', 'book_evidence_urls', 'book_evidence_versions')):
            raise ValueError('Complete original title, version, and reading identity are required')
        result[key] = copy.deepcopy(record) | {'source': {field: source[field] for field in (
            'source_url', 'embedded_report_url', 'fetched_on', 'source_document_sha256', 'registrar_document_sha256')}}
    return result


def load_aliases(path):
    path = Path(path)
    return validate_aliases(json.loads(path.read_text())) if path.exists() else {}


def aliases_for(original, aliases):
    if not original.get('course_key') and original.get('course_id'):
        original = original | {'course_key': 'topclass-v0.4:' + original['course_id'], 'code': original.get('course_code')}
    record = aliases.get(original.get('course_key'))
    if record is None:
        return {}
    if any(original.get(key) != value for key, value in record['identity'].items()):
        raise ValueError('Course alias binding differs from original source identity')
    return {'course_code_aliases': [record['old_code'], record['new_code'], record['old_code'].replace(' ', ''), record['new_code'].replace(' ', '')],
            'course_code_alias_evidence': [copy.deepcopy(record)]}


def collect(out):
    from bs4 import BeautifulSoup
    from catalog import load_catalog

    if out.exists():
        raise FileExistsError('Use a new alias receipt output path')
    registrar = urllib.request.urlopen(REGISTRAR, timeout=25).read(1000000)
    soup = BeautifulSoup(registrar, 'html.parser')
    if not any(frame.get('src') == PUBLIC_REPORT for frame in soup.find_all('iframe')):
        raise ValueError('Public Registrar report referral changed')
    headers = {'X-PowerBI-ResourceKey': RESOURCE, 'Content-Type': 'application/json'}
    metadata_url = PUBLIC_API + '/public/reports/' + RESOURCE + '/modelsAndExploration?preferReadOnlySession=true'
    metadata = json.load(urllib.request.urlopen(urllib.request.Request(metadata_url, headers=headers), timeout=25))
    query = next(json.loads(visual['query']) for visual in metadata['exploration']['sections'][0]['visualContainers']
                 if 'New Course #' in visual.get('query', '') and 'Course Title' in visual.get('query', ''))
    command = query['Commands'][0]['SemanticQueryDataShapeCommand']
    command['Query']['Where'] = [{'Condition': {'In': {'Expressions': [
        {'Column': {'Expression': {'SourceRef': {'Source': '3'}}, 'Property': name}}
        for name in ('Old Subj. Code', 'Old Course #')], 'Values': [
            [{'Literal': {'Value': "'CPSC'"}}, {'Literal': {'Value': repr(code)}}] for code in ('223', '365')]}}}]
    body = {'version': '1.0.0', 'queries': [{'Query': query, 'ApplicationContext': {'DatasetId': metadata['models'][0]['dbName']}}],
            'cancelQueries': [], 'modelId': metadata['models'][0]['id']}
    request = urllib.request.Request(PUBLIC_API + '/public/reports/querydata?synchronous=true',
                                     data=json.dumps(body).encode(), headers=headers)
    raw = urllib.request.urlopen(request, timeout=25).read(1000000).decode()
    rows = decode_rows(json.loads(raw))
    records = []
    for original in load_catalog()[0]['courses']:
        if original['course_id'] not in {'C224', 'C232'}:
            continue
        row = next(row for row in rows if row['New Subj Code'] + ' ' + row['New Course #'] == original['course_code'])
        binding = {key: original[key] for key in ('title', 'source_url', 'version', 'book_evidence_urls', 'book_evidence_versions')}
        binding.update(course_key='topclass-v0.4:' + original['course_id'], code=original['course_code'])
        records.append({'identity': binding, 'old_code': row['Old Subj. Code'] + ' ' + row['Old Course #'],
                        'new_code': original['course_code'], 'registrar_row': row,
                        'scope': 'Course-number equivalence only; historical book adoption and teaching content are unchanged.'})
    payload = {'format': 'topclass-course-code-aliases-v1', 'records': records,
               'source': {'source_url': REGISTRAR, 'embedded_report_url': PUBLIC_REPORT,
                          'registrar_document_sha256': hashlib.sha256(registrar).hexdigest(),
                          'source_document_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                          'fetched_on': date.today().isoformat(), 'response_json': raw}}
    validate_aliases(payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(json.dumps({'mappings': len(records), 'source': REGISTRAR}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Capture only two explicitly reviewed Yale Registrar public course-code equivalences.')
    parser.add_argument('--out', required=True, type=Path)
    collect(parser.parse_args().out)
