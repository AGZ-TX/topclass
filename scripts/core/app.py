#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import uuid

from core.catalog import CatalogLibrary, export_catalog
from core.hire import agent_plan
from core.search import configured_repository, replace_json, run_search, search_request, validate_repository
from course_discovery import ROOT, load_discovery
from core.graph import Graph
from core.planner import apply_selection, planner_data, selection_manifest
from core.reports import render_report
from core.presentation import TEMPLATE as HIRE_TEMPLATE, check_presentation
from core.brief import interpret_description, normalize_brief
from core.recall import navigation, read_region, search_sources
from core.paths import default_home, private_home
from core.system import lock_file, skill_content


DEFAULT_HOME = default_home(ROOT)
ONBOARDING = "What is your agent's purpose?\nFor example: “I want an agent to develop my brand and guide its design.”"


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    with Path(path).open('x', encoding='utf-8') as stream:
        Path(path).chmod(0o600)
        stream.write(content)


def child(parent, *parts):
    parent = Path(parent).absolute()
    path = parent
    for part in parts:
        if not isinstance(part, str) or not part or Path(part).name != part or part in {'.', '..'}:
            raise ValueError('Invalid private workspace path')
        path = path / part
        if path.is_symlink():
            raise ValueError('Agent workspaces cannot use symlinks')
    if not path.resolve().is_relative_to(parent.resolve()):
        raise ValueError('Path leaves the agent workspace')
    return path


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('Use the agent ID shown by the agents command')
    return value


@contextmanager
def locked(home):
    home = private_home(ROOT, home)
    if home.is_symlink():
        raise ValueError('The agents directory cannot be a symlink')
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with child(home, '.lock').open('a') as stream:
        Path(stream.name).chmod(0o600)
        lock_file(stream)
        yield home


def agent_at(home, agent_id):
    folder = child(home, identifier(agent_id))
    agent = read_json(child(folder, 'agent.json'))
    if agent.get('id') != agent_id or agent.get('format') != 'topclass-agent-v1':
        raise ValueError('Agent identity does not match its workspace')
    return folder, agent


def memory(folder, agent):
    path = child(folder, 'knowledge.db')
    if not path.is_file():
        raise ValueError('Agent memory is missing; it was not recreated')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        owner = db.execute("SELECT value FROM meta WHERE key='agent_id'").fetchone()
    if not owner or owner[0] != agent['id']:
        raise ValueError('Memory belongs to a different agent')
    graph = Graph(path)
    try:
        sources = child(folder, 'sources').resolve()
        for row in graph.db.execute("SELECT payload FROM nodes WHERE kind IN ('source','region')"):
            payload = json.loads(row['payload'])
            for field in ('original_path', 'image_path'):
                if payload.get(field) and not Path(payload[field]).resolve().is_relative_to(sources):
                    raise ValueError('Source memory points outside this agent\'s retained sources')
        return graph
    except BaseException:
        graph.close()
        raise


def replace_state(folder, value):
    path = child(folder, 'state.json')
    temporary = child(folder, uuid.uuid4().hex + '.json')
    save(temporary, value)
    temporary.replace(path)


def library_for(home, agent):
    key = agent.get('catalog_id')
    if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key):
        raise ValueError('Agent has no valid catalog snapshot')
    return CatalogLibrary(child(child(home, '.catalogs'), key), agent['catalog_manifest_hash'])


def write_bundle(folder, bundle):
    for name, value in bundle.items():
        save(folder / (name + '.json'), value)
    save(folder / 'planner-data.json', json.dumps(planner_data(bundle['plan'], bundle['candidates'], presentation=True), ensure_ascii=False, separators=(',', ':')))
    save(folder / 'choices.json', selection_manifest(bundle['plan'], bundle['candidates']))


def hire(home, description, name=None, brief=None, catalog=None, baseline_only=False):
    if not isinstance(description, str) or not description.strip() or len(description.encode('utf-8')) > 32768:
        raise ValueError('Describe the agent purpose in nonempty text of at most 32 KiB')
    description = description.strip()
    brief = normalize_brief(brief) if brief is not None else interpret_description(description)
    if brief.get('original_description') != description:
        raise ValueError('The brief must preserve the exact original description')
    agent = {'format': 'topclass-agent-v1', 'id': uuid.uuid4().hex,
             'name': name or brief.get('role') or 'New agent', 'description': description,
             'memory_scope': 'this agent only'}
    if not isinstance(agent['name'], str) or not agent['name'].strip() or len(agent['name']) > 200:
        raise ValueError('Choose an agent name of 1 to 200 characters')
    with locked(home) as home:
        catalog = catalog or load_discovery(baseline_only=baseline_only)
        agent['catalog_id'], agent['catalog_manifest_hash'] = export_catalog(catalog, child(home, '.catalogs'))
        bundle = agent_plan(library_for(home, agent), brief, agent)
        with tempfile.TemporaryDirectory(prefix='.hire-', dir=home) as staging:
            folder = Path(staging) / agent['id']
            folder.mkdir(mode=0o700)
            for name in ('sources', 'pageindex', 'inbox', 'plans', 'selections'):
                (folder / name).mkdir(mode=0o700)
            graph = Graph(folder / 'knowledge.db')
            try:
                with graph.db:
                    graph.db.execute('INSERT INTO meta(key,value) VALUES(?,?)', ('agent_id', agent['id']))
            finally:
                graph.close()
            (folder / 'knowledge.db').chmod(0o600)
            save(folder / 'agent.json', agent)
            plan_id = uuid.uuid4().hex
            plan_dir = folder / 'plans' / plan_id
            plan_dir.mkdir(mode=0o700)
            write_bundle(plan_dir, bundle)
            save(folder / 'state.json', {'stage': 'awaiting-ai-selection', 'plan_id': plan_id, 'selection_id': None,
                                        'baseline_only': baseline_only})
            folder.rename(home / agent['id'])
    return status(home, agent['id'])


def current_plan(folder):
    state = read_json(child(folder, 'state.json'))
    plan_dir = child(child(folder, 'plans'), identifier(state['plan_id']))
    return state, plan_dir


def status(home, agent_id):
    from core.pipeline import pipeline_status, progress_message
    folder, agent = agent_at(home, agent_id)
    state, plan_dir = current_plan(folder)
    graph = memory(folder, agent)
    try:
        counts = graph.stats()['nodes']
    finally:
        graph.close()
    result = {'agent_id': agent_id, 'name': agent['name'], 'stage': state['stage'],
              'education_template': str(HIRE_TEMPLATE), 'education_html': str(plan_dir / 'education.html'), 'brief': str(plan_dir / 'brief.json'),
              'proposal': str(plan_dir / 'proposal.json'), 'planner_data': str(plan_dir / 'planner-data.json'),
              'catalog_manifest': str(child(child(child(home, '.catalogs'), agent['catalog_id']), 'catalog.json')),
              'catalog_jsonl': str(child(child(child(home, '.catalogs'), agent['catalog_id']), 'courses.jsonl')),
              'knowledge_graph': str(folder / 'knowledge.db'), 'pageindex_storage': str(child(folder, 'pageindex')),
              'registered_sources': counts.get('source', 0), 'knowledge_records': counts.get('knowledge', 0),
              'queued_materials': len(list(child(folder, 'inbox').glob('*/receipt.json'))),
              'next': 'Open the education HTML. Choose courses, save your choices, and return the choices file to your agent.'}
    if state.get('selection_id'):
        selection = child(child(folder, 'selections'), identifier(state['selection_id']))
        result.update({'education_html': str(selection / 'education.html'), 'planner_data': str(selection / 'planner-data.json'), 'book_report': str(selection / 'books.md'),
                       'next': 'Review your selected courses and book list. You can revise the choices in the education HTML.'})
        if state['stage'] == 'no-courses-selected':
            result['next'] = 'No courses selected. Reopen the education HTML to add courses or refine the agent purpose.'
        request = child(selection, 'book-search-request.json')
        search = child(selection, 'book-search.json')
        if request.is_file():
            result['book_search_request'] = str(request)
        if search.is_file():
            result['book_search_report'] = str(search)
            result['book_search'] = read_json(search)
    result['education_html_exists'] = Path(result['education_html']).is_file()
    if not result['education_html_exists']:
        result['next'] = ('The AI must choose courses from the catalog and author the education HTML.'
                          if state['stage'] == 'awaiting-ai-selection' else
                          'The course choices are saved. The AI must author the education HTML from the returned planner data.')
    elif result.get('book_search') and state['stage'] != 'no-courses-selected':
        search = result['book_search']
        result['next'] = ('The book search is complete. Review the checked files and any reference-edition notes; no books have been learned.'
                          if search['status'] == 'complete' else
                          'Review the book search results and remaining source links. Your confirmed course choices are saved.')
    indexing = pipeline_status(home, agent_id)
    if indexing:
        result['source_indexing'] = indexing
        result['next'] = progress_message(indexing)
    return result


def revise(home, agent_id, brief=None, proposal=None):
    with locked(home) as home:
        folder, agent = agent_at(home, agent_id)
        state, old_plan = current_plan(folder)
        if proposal is None and brief is None:
            proposal = read_json(old_plan / 'proposal.json')
        brief = brief if brief is not None else read_json(old_plan / 'brief.json')
        bundle = agent_plan(library_for(home, agent), brief, agent, proposal)
        plan_id = uuid.uuid4().hex
        plan_dir = child(child(folder, 'plans'), plan_id)
        plan_dir.mkdir(mode=0o700)
        write_bundle(plan_dir, bundle)
        stage = 'choose-courses' if bundle['plan']['courses'] else 'awaiting-ai-selection'
        replace_state(folder, state | {'stage': stage, 'plan_id': plan_id, 'selection_id': None})
    return status(home, agent_id)


def select_courses(home, agent_id, selection, search_repo=None):
    with locked(home) as home:
        folder, agent = agent_at(home, agent_id)
        state, plan_dir = current_plan(folder)
        plan, candidates = read_json(plan_dir / 'plan.json'), read_json(plan_dir / 'candidates.json')
        selected = apply_selection(plan, selection, candidates)
        selection_id = uuid.uuid4().hex
        output = child(child(folder, 'selections'), selection_id)
        output.mkdir(mode=0o700)
        save(output / 'choices.json', selection)
        save(output / 'education.json', selected)
        save(output / 'planner-data.json', json.dumps(planner_data(plan, candidates, selection, presentation=True), ensure_ascii=False, separators=(',', ':')))
        save(output / 'books.md', render_report(selected))
        save(output / 'book-search-request.json', search_request(selected, agent_id, selection_id))
        stage = 'awaiting-materials' if selected['courses'] else 'no-courses-selected'
        replace_state(folder, state | {'stage': stage, 'selection_id': selection_id})
    return find_books(home, agent_id, search_repo)


def configure_search(home, repository):
    root = validate_repository(repository)
    with locked(home) as home:
        replace_json(child(home, '.search.json'), {'format': 'topclass-book-search-config-v1', 'repository_path': str(root)})
    return {'repository': 'https://github.com/AGZ-TX/search', 'repository_path': str(root),
            'next': 'Confirmed course choices will automatically run this book search.'}


def find_books(home, agent_id, search_repo=None):
    with locked(home) as home:
        folder, agent = agent_at(home, agent_id)
        graph = memory(folder, agent)
        graph.close()
        state, _ = current_plan(folder)
        if not state.get('selection_id'):
            raise ValueError('Confirm course choices in the education HTML before searching for books')
        selection = child(child(folder, 'selections'), identifier(state['selection_id']))
        request = search_request(read_json(child(selection, 'education.json')), agent_id, state['selection_id'])
        saved = child(selection, 'book-search-request.json')
        if saved.exists() and read_json(saved) != request:
            raise ValueError('Book search request differs from the confirmed selection')
        if not saved.exists():
            save(saved, request)
        problem = None
        try:
            repository = configured_repository(home, ROOT, search_repo) if request['isbns'] else None
        except (ValueError, OSError, KeyError) as error:
            repository, problem = None, str(error)
    outcome = run_search(selection, request, repository)
    if problem:
        outcome['reason'] = problem
        replace_json(child(selection, 'book-search.json'), outcome)
    if outcome.get('files'):
        from core.pipeline import enqueue_search
        enqueue_search(home, agent_id, selection, outcome)
    return status(home, agent_id)


def check_html(home, agent_id):
    current = status(home, agent_id)
    html = Path(current['education_html']).read_text(encoding='utf-8')
    presentation = check_presentation(html, read_json(current['planner_data']))
    return {'agent_id': agent_id, 'education_html': current['education_html'],
            'data_matches': True, **presentation,
            'limits': 'Checks approved structure and validated data; verify browser behavior separately.'}


def add_material(home, agent_id, path, title=None, edition='Not stated', origin=''):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError('Provide a local material file, not a link or folder')
    with locked(home) as home:
        folder, agent = agent_at(home, agent_id)
        graph = memory(folder, agent)
        graph.close()
        with tempfile.TemporaryDirectory(prefix='.add-', dir=folder) as staging:
            staged = Path(staging)
            original = staged / ('original' + path.suffix.lower())
            digest = hashlib.sha256()
            with path.open('rb') as source, original.open('xb') as target:
                original.chmod(0o600)
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(block)
                    target.write(block)
            assets = None
            if path.suffix.casefold() in {'.html', '.htm'}:
                from core.markup import stage_assets
                assets = stage_assets(path, staged)
            receipt = {'agent_id': agent_id, 'sha256': digest.hexdigest(), 'title': title or path.name,
                       'edition': edition, 'origin': origin, 'original_name': path.name,
                       'file': original.name, 'status': 'queued; not digested', 'untrusted_evidence': True}
            key = hashlib.sha256(json.dumps([receipt['sha256'], receipt['title'], edition, origin, assets] if assets is not None else [receipt['sha256'], receipt['title'], edition, origin]).encode()).hexdigest()
            destination = child(child(folder, 'inbox'), key)
            if destination.exists():
                receipt = read_json(child(destination, 'receipt.json')) | {'duplicate': True}
            else:
                save(staged / 'receipt.json', receipt)
                staged.rename(destination)
    result = receipt | {'inbox': str(destination), 'next': 'Saved only for this agent. Set up your Google key to index this material.'}
    from core.pipeline import launch_worker, queue_material
    result['index_job'] = queue_material(home, agent_id, child(destination, receipt['file']),
                                         receipt['title'], receipt['edition'], receipt['origin'], receipt['sha256'])
    result['processing'] = launch_worker(home, agent_id)
    if result['index_job']['state'] in {'unsupported', 'excluded'}:
        result['next'] = 'Saved privately; not indexed. ' + result['index_job']['reason']
    elif child(home, '.google.json').is_file():
        result['next'] = 'The original material is being indexed for this agent. Use /recall to search completed passages.'
    return result


def recall(home, agent_id, query=None, region=None, source=None, limit=5, budget=12000, page=None, section=None, output_budget=24000, visual=False, bbox=None, start_region=None, offset=0):
    from core.pipeline import google_provider, launch_worker
    from core.provider import ProviderDeferred, ProviderFailure

    folder, agent = agent_at(home, agent_id)
    if child(folder, 'index-queue.json').is_file():
        launch_worker(home, agent_id)
    graph = memory(folder, agent)
    try:
        if (start_region is not None or offset) and section is None:
            raise ValueError('--start and --offset require a section')
        if section is not None:
            from core.recall import read_section
            if not source or query or region or page is not None or visual or bbox is not None:
                raise ValueError('--section requires --source without --query, --region or --page')
            result = read_section(graph, source, section, budget, start_region, offset)
        elif visual:
            from core.recall import visual_region
            if not region or query or source or page is not None:
                raise ValueError('--visual requires --region without other input selectors')
            result = visual_region(graph, region, bbox)
        elif bbox is not None:
            raise ValueError('--crop requires --visual and --region')
        elif page is not None:
            from core.recall import read_page
            if not source or query or region:
                raise ValueError('--page requires --source without --query or --region')
            result = read_page(graph, source, page)
        elif region:
            result = read_region(graph, region)
        elif source:
            result = navigation(graph, source)
        else:
            vector, space, search_gap, retry_at = None, None, None, None
            if child(home, '.google.json').is_file() and graph.db.execute('SELECT 1 FROM vectors').fetchone():
                try:
                    with google_provider(home, graph, max_requests=1) as provider:
                        if graph.db.execute('SELECT 1 FROM vectors WHERE space=?', (provider.space,)).fetchone():
                            vector, space = provider.query(query), provider.space
                except (ProviderDeferred, ProviderFailure, ValueError, OSError) as error:
                    search_gap = str(error)
                    if isinstance(error, ProviderDeferred):
                        retry_at = error.next_attempt_at
            result = search_sources(graph, query, vector=vector, space=space, limit=limit, budget=budget)
            result['semantic_search'] = vector is not None
            if search_gap:
                result['gaps'].append({'kind': 'semantic-query', 'reason': search_gap,
                                       **({'next_attempt_at': retry_at} if retry_at is not None else {})})
            elif vector is None:
                result['gaps'].append({'kind': 'semantic-index', 'reason': 'No compatible query vector is available; these results use original-text search.'})
            sources = {row['source_id'] for row in result['results']}
            from core.health import review
            result['indexing'] = review(graph, sorted(sources), validate=False)
            result['navigation'] = [navigation(graph, sid, pages=[row.get('physical_page', graph.get(row['region_id'])['payload']['region_index'] + 1) for row in result['results'] if row['source_id'] == sid]) for sid in sorted(sources)]
            if not result['results']:
                result['message'] = 'No matching source knowledge is available for this agent. Queued files and course book lists are not learned knowledge.'
        from core.output import bounded
        answer = {'agent_id': agent_id, **result}
        return bounded(answer, output_budget) if query or section else answer
    finally:
        graph.close()


def install_skills(destination, host):
    destination = Path(destination).absolute()
    base = child(destination, '.agents' if host == 'codex' else '.claude', 'skills')
    paths = [child(base, name) for name in ('hire', 'recall', 'add')]
    if any(path.exists() for path in paths):
        raise ValueError('A destination skill already exists; keep it or remove it explicitly before installing')
    for path in paths:
        path.mkdir(parents=True)
        save(path / 'SKILL.md', skill_content(ROOT, path.name))
    return {'installed': [str(path) for path in paths], 'host': host,
            'next': 'Reload host skills. Claude uses /hire; Codex uses $hire or its skills menu. In this repository, /hire also routes through AGENTS.md.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Hire an agent, choose its education, and keep its memory private.')
    parser.add_argument('--home', type=Path, default=DEFAULT_HOME, help='Private agent workspaces')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('hire', help='Describe your agent purpose and open its course library')
    inputs = p.add_mutually_exclusive_group()
    inputs.add_argument('--description')
    inputs.add_argument('--description-file', type=Path)
    p.add_argument('--brief', type=Path, help='Host-interpreted skills, priorities, and assumptions')
    p.add_argument('--name')
    p.add_argument('--baseline-only', action='store_true')
    sub.add_parser('agents', help='List agent IDs and names')
    for name in ('status', 'catalog', 'browse', 'inspect', 'plan', 'select', 'find-books', 'check-html', 'add', 'recall', 'work', 'review', 'live'):
        aliases = {'find-books': ['books'], 'check-html': ['check']}.get(name, [])
        p = sub.add_parser(name, aliases=aliases)
        p.add_argument('--agent', required=True, help='Agent ID; there is no shared default memory')
        if name == 'live':
            p.add_argument('--port', type=int, default=0)
        elif name == 'plan':
            p.add_argument('--brief', type=Path)
            p.add_argument('--proposal', type=Path, help='AI-authored course choices, priorities, and source reviews')
        elif name == 'browse':
            p.add_argument('--field')
            p.add_argument('--university')
            p.add_argument('--contains', help='AI-chosen literal text filter; no relevance ranking')
            p.add_argument('--offset', type=int, default=0)
            p.add_argument('--limit', type=int, default=25)
        elif name == 'inspect':
            p.add_argument('course_ids', nargs='+')
        elif name == 'select':
            p.add_argument('choices', type=Path)
            p.add_argument('--search-repo', type=Path, help='Prepared AGZ-TX/search checkout; otherwise use saved configuration')
        elif name == 'find-books':
            p.add_argument('--search-repo', type=Path, help='Prepared AGZ-TX/search checkout for retrying confirmed books')
        elif name == 'add':
            p.add_argument('file', type=Path)
            p.add_argument('--title')
            p.add_argument('--edition', default='Not stated')
            p.add_argument('--origin', default='')
        elif name == 'recall':
            p.add_argument('--page', type=int, help='Read a physical page from --source')
            p.add_argument('--limit', type=int, default=5)
            p.add_argument('--budget', type=int, default=12000, help='Maximum original-text characters in query or section results')
            p.add_argument('--output-budget', type=int, default=24000, help='Maximum complete query or section JSON size in UTF-8 bytes')
            p.add_argument('--section', help='Open a section node from --source navigation')
            p.add_argument('--start', help='Continue a section at the returned region ID')
            p.add_argument('--offset', type=int, default=0, help='Continue a section at the returned exact character offset')
            p.add_argument('--visual', action='store_true', help='Render an original PDF page or crop at higher resolution with --region')
            p.add_argument('--crop', type=float, nargs=4, metavar=('X0', 'Y0', 'X1', 'Y1'))
            inputs = p.add_mutually_exclusive_group(required=True)
            inputs.add_argument('--query')
            inputs.add_argument('--region')
            inputs.add_argument('--source')
    p = sub.add_parser('install-skills', aliases=['skills'])
    p.add_argument('--host', choices=('codex', 'claude'), required=True)
    p.add_argument('--destination', type=Path, default=ROOT)
    p = sub.add_parser('configure-search', aliases=['finder'], help='Connect a prepared book finder')
    p.add_argument('--repo', type=Path, required=True)
    p = sub.add_parser('configure-google', aliases=['google'], help='Set up your private Google API key')
    p.add_argument('--fallback', action='store_true', help='Add a backup key without replacing the primary')
    p.add_argument('--key-file', type=Path, help='Private plain-text key file; otherwise use the environment or a hidden prompt')
    args = parser.parse_args(argv)
    args.command = {'books': 'find-books', 'check': 'check-html', 'skills': 'install-skills',
                    'finder': 'configure-search', 'google': 'configure-google'}.get(args.command, args.command)
    if args.command == 'hire':
        brief = read_json(args.brief) if args.brief else None
        description = args.description_file.read_text(encoding='utf-8').strip() if args.description_file else args.description
        description = description or (brief or {}).get('original_description')
        if not description:
            print(ONBOARDING)
            if not sys.stdin.isatty():
                return 0
            try:
                description = input('> ').strip()
            except EOFError:
                return 0
        print('Opening the saved course library for your agent…', file=sys.stderr)
        result = hire(args.home, description, args.name, brief, baseline_only=args.baseline_only)
    elif args.command == 'agents':
        result = []
        if args.home.exists():
            for path in sorted(args.home.iterdir()):
                if re.fullmatch(r'[a-f0-9]{32}', path.name):
                    _, agent = agent_at(args.home, path.name)
                    result.append({'agent_id': agent['id'], 'name': agent['name']})
    elif args.command == 'status':
        result = status(args.home, args.agent)
    elif args.command == 'plan':
        result = revise(args.home, args.agent, read_json(args.brief) if args.brief else None,
                        read_json(args.proposal) if args.proposal else None)
    elif args.command in ('catalog', 'browse', 'inspect'):
        _, agent = agent_at(args.home, args.agent)
        library = library_for(args.home, agent)
        if args.command == 'catalog':
            result = library.manifest
        elif args.command == 'browse':
            result = library.browse(args.field, args.university, args.contains, args.offset, args.limit)
        else:
            result = [library.inspect(key) for key in args.course_ids]
    elif args.command == 'check-html':
        result = check_html(args.home, args.agent)
    elif args.command == 'select':
        result = select_courses(args.home, args.agent, read_json(args.choices), args.search_repo)
    elif args.command == 'find-books':
        result = find_books(args.home, args.agent, args.search_repo)
    elif args.command == 'configure-search':
        result = configure_search(args.home, args.repo)
    elif args.command == 'configure-google':
        from core.pipeline import configure_google
        result = configure_google(args.home, args.key_file, fallback=args.fallback)
    elif args.command == 'review':
        from core.health import review
        folder, agent = agent_at(args.home, args.agent)
        graph = memory(folder, agent)
        try:
            result = {'agent_id': args.agent, **review(graph)}
        finally:
            graph.close()
    elif args.command == 'live':
        from core.live import serve
        serve(args.home, args.agent, args.port)
        return 0
    elif args.command == 'work':
        from core.pipeline import run_worker
        result = run_worker(args.home, args.agent)
    elif args.command == 'add':
        result = add_material(args.home, args.agent, args.file, args.title, args.edition, args.origin)
    elif args.command == 'recall':
        result = recall(args.home, args.agent, args.query, args.region, args.source, args.limit, args.budget, args.page, args.section, args.output_budget, args.visual, args.crop, args.start, args.offset)
    else:
        result = install_skills(args.destination, args.host)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, sqlite3.Error) as error:
        print('Error: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
