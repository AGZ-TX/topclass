from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from core.catalog import readable_course
from education_selection import acquisition_plan, has_named_material, requirement_coverage

FORMAT = 'topclass-course-selection-v1'
TEMPLATE = Path(__file__).resolve().parents[2] / 'docs/templates/education.html'


def course_pool(plan, candidates=None):
    if not isinstance(plan, dict) or not isinstance(plan.get('courses', []), list):
        raise ValueError('Planner needs an education plan with a course list')
    extra = candidates.get('courses', []) if isinstance(candidates, dict) else candidates or []
    if not isinstance(extra, list):
        raise ValueError('Planner alternatives must be a course list or education plan')
    pool = {}
    for item in [*plan.get('courses', []), *extra]:
        if not isinstance(item, dict) or not isinstance(item.get('course'), dict):
            raise ValueError('Planner course rows must contain course evidence')
        key = item['course'].get('id')
        if not isinstance(key, str) or not key or not has_named_material(item.get('materials_evidence', [])):
            raise ValueError('Every planner course must have a stable ID and documented named book')
        if key not in pool:
            pool[key] = copy.deepcopy(item)
    return pool


def selection_manifest(plan, candidates=None):
    pool = course_pool(plan, candidates)
    evidence_plan = copy.deepcopy(plan)
    if isinstance(evidence_plan.get('snapshot'), dict):
        evidence_plan['snapshot'].pop('build_seconds', None)
    digest = hashlib.sha256(json.dumps({'plan': evidence_plan, 'pool': pool}, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {'format': FORMAT, 'plan_fingerprint': digest, 'snapshot_fingerprint': (plan.get('snapshot') or {}).get('fingerprint'),
            'courses': [{'course_id': item['course']['id'], 'priority': item['selection_role']} for item in plan.get('courses', [])]}


def apply_selection(plan, selection, candidates=None):
    expected = selection_manifest(plan, candidates)
    if not isinstance(selection, dict) or set(selection) != set(expected):
        raise ValueError('Selection must contain only the exported manifest fields')
    if any(selection.get(key) != value for key, value in expected.items() if key != 'courses'):
        raise ValueError('Selection belongs to a different plan, evidence pool, or catalog snapshot')
    requested = selection.get('courses')
    if not isinstance(requested, list):
        raise ValueError('Selection courses must be a list')
    pool = course_pool(plan, candidates)
    chosen, seen = [], set()
    original_ids = {item['course']['id'] for item in plan.get('courses', [])}
    for item in requested:
        if not isinstance(item, dict) or set(item) not in ({'course_id'}, {'course_id', 'priority'}):
            raise ValueError('Only course IDs can be selected; legacy priority is accepted for compatibility')
        key, priority = item['course_id'], item.get('priority', 'must-have')
        if not isinstance(key, str) or key not in pool or key in seen or not isinstance(priority, str) or priority not in {'must-have', 'supplemental'}:
            raise ValueError('Unknown, duplicate, or invalid course selection')
        seen.add(key)
        course = copy.deepcopy(pool[key])
        course['selection_role'] = priority
        if key not in original_ids:
            course['selection_review_state'] = 'user-added; task fit needs review'
        chosen.append(course)
    result = copy.deepcopy(plan)
    result['original_recommendation'] = copy.deepcopy(plan.get('original_recommendation', plan))
    result['user_selection'] = copy.deepcopy(selection)
    result['courses'] = chosen
    result['must_have_courses'] = [item for item in chosen if item['selection_role'] == 'must-have']
    result['supplemental_courses'] = [item for item in chosen if item['selection_role'] == 'supplemental']
    gaps = [copy.deepcopy(item) for item in plan.get('gaps', []) if item.get('constraint')]
    requirements = [(item['kind'], item['requirement']) for item in plan.get('requirement_coverage', [])]
    result['requirement_coverage'] = requirement_coverage(chosen, requirements, gaps)
    result['materials_to_supply'], result['unidentified_materials'] = acquisition_plan(chosen, gaps)
    result['gaps'] = gaps
    result['status'] = 'user-selected education; review open gaps and bibliography before supply'
    return result


def planner_data(plan, candidates=None, selection=None, presentation=False):
    pool = course_pool(plan, candidates)
    manifest = selection_manifest(plan, candidates)
    recommendation_courses = copy.deepcopy(manifest['courses'])
    if selection is not None:
        apply_selection(plan, selection, candidates)
        manifest['courses'] = copy.deepcopy(selection['courses'])
    catalog = [readable_course(item['course'], item['materials_evidence']) for item in pool.values()]
    requirements = [(item['kind'], item['requirement']) for item in plan.get('requirement_coverage', [])]
    coverage = requirement_coverage(list(pool.values()), requirements, [])
    result = {'plan': plan, 'manifest': manifest, 'recommendation_courses': recommendation_courses,
              'catalog': catalog, 'coverage': coverage}
    if presentation:
        return result
    material_views = {}
    for key, item in pool.items():
        material_views[key] = {}
        for priority in ('must-have', 'supplemental'):
            view = copy.deepcopy(item)
            view['selection_role'] = priority
            material_views[key][priority] = acquisition_plan([view], [])[0]
    return result | {'pool': pool, 'materials': material_views}


def render_planner(plan, candidates=None, selection=None):
    data = planner_data(plan, candidates, selection)
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    return TEMPLATE.read_text(encoding='utf-8').replace('__PLANNER_DATA__', encoded)
