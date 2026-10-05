from __future__ import annotations

import copy

from education_selection import (acquisition_plan, classification_reliability, has_named_material,
                                  program_roles, requirement_coverage, source_quote_lineage, validate_course_reviews)
from core.brief import normalize_brief
from expert_education import requirements_for


def agent_plan(library, brief, agent, proposal=None):
    brief = normalize_brief(brief)
    if brief.get('original_description') != agent['description']:
        raise ValueError('Keep the original user description in the brief')
    proposal = {'courses': []} if proposal is None else proposal
    if not isinstance(proposal, dict) or set(proposal) != {'courses'} or not isinstance(proposal['courses'], list):
        raise ValueError('AI proposal must contain a courses list')
    requirements = requirements_for(brief)
    kinds = {requirement: kind for kind, requirement in requirements}
    selected, alternatives, seen = [], [], set()
    for choice in proposal['courses']:
        if not isinstance(choice, dict) or set(choice) != {'course_id', 'priority', 'reviews'}:
            raise ValueError('Each AI choice needs course_id, priority and source-anchored reviews')
        key, priority, reviews = choice['course_id'], choice['priority'], choice['reviews']
        if not isinstance(key, str) or key in seen or priority not in ('must-have', 'supplemental', 'alternative'):
            raise ValueError('Unknown priority or duplicate course choice')
        seen.add(key)
        if not isinstance(reviews, list) or not reviews or any(not isinstance(review, dict) or review.get('course_id') != key for review in reviews):
            raise ValueError('Every AI choice needs reviews for that exact course')
        if len({review.get('requirement') for review in reviews}) != len(reviews):
            raise ValueError('Use one decision per course and requirement')
        validate_course_reviews(library, brief | {'course_reviews': reviews}, requirements)
        evidence = library.evidence(key)
        node, materials = evidence['course'], evidence['materials']
        if not has_named_material(materials):
            raise ValueError('The chosen course has no documented named book')
        matches, reasons = [], []
        for review in reviews:
            if review['decision'] == 'reject':
                continue
            requirement = review['requirement']
            anchors = [{'field': anchor['field'], 'text': anchor['quote'], 'basis': 'source-field'}
                       for anchor in [review, *review.get('supporting_evidence', [])]]
            for anchor in anchors:
                anchor['source_lineage'] = source_quote_lineage(node['payload'], anchor)
            matches.append({'kind': kinds[requirement], 'requirement': requirement, 'coverage_fraction': 1.0,
                            'missing_terms': [], 'evidence': anchors, 'review_decision': review['decision'],
                            'support_level': 'direct' if review['decision'] == 'essential' else 'supporting',
                            'review_state': 'host-reviewed', 'relevance_method': 'AI source-anchored judgment',
                            'fit_basis': 'AI judgment with a validated source quote; not verified outcomes'})
            reasons.append({'requirement': requirement, 'rationale': review['rationale'], 'evidence': anchors,
                            'selection_kind': 'AI choice', 'outcomes_verified': False})
        if not matches:
            raise ValueError('A recommended course needs at least one non-rejected source review')
        row = {'course': node, 'selection_role': 'supplemental' if priority == 'alternative' else priority,
               'matches': matches, 'selection_rationale': reasons, 'selection_review_state': 'host-reviewed',
               'materials_evidence': materials, 'classification_reliability': classification_reliability(node['payload']),
               'prerequisites': node['payload'].get('prerequisites') or 'Not mapped',
               'program_core_or_elective': program_roles(node['payload']) or 'Not mapped to a documented program'}
        (alternatives if priority == 'alternative' else selected).append(row)
    if selected:
        for record in library.rows:
            key = record['course_id']
            if key in seen:
                continue
            evidence = library.evidence(key)
            node, materials = evidence['course'], evidence['materials']
            if not has_named_material(materials):
                continue
            payload_fields = {'title', 'institution', 'school', 'code', 'course_key', 'course_code_aliases',
                              'source_course_keys', 'source_urls', 'source_url', 'description', 'description_variants',
                              'academic_areas', 'discovery_academic_areas', 'prerequisites'}
            compact_node = {'id': node['id'], 'kind': node['kind'], 'label': node['label'],
                            'payload': {name: value for name, value in node['payload'].items() if name in payload_fields}}
            status_fields = {'status', 'source_course_key', 'materials', 'sources', 'gaps', 'checked_on', 'source_year'}
            compact_materials = [{name: value for name, value in status.items() if name in status_fields} for status in materials]
            alternatives.append({'course': compact_node, 'materials_evidence': compact_materials,
                                 'catalog_evidence': {'file': record['evidence_file'], 'sha256': record['evidence_sha256']},
                                 'selection_role': 'supplemental',
                                 'matches': [], 'selection_rationale': [],
                                 'selection_review_state': 'catalog-only; task fit unreviewed',
                                 'prerequisites': node['payload'].get('prerequisites') or 'Not mapped',
                                 'program_core_or_elective': program_roles(node['payload']) or 'Not mapped to a documented program'})
    gaps = [{'constraint': value, 'gap': 'Keep this in mind when reviewing the course choices.'}
            for value in brief.get('constraints', [])]
    gaps += [{'constraint': value, 'gap': 'Background provided for choosing relevant courses.'}
             for value in brief.get('existing_knowledge', [])]
    coverage = requirement_coverage(selected, requirements, gaps)
    books, unresolved = acquisition_plan(selected, gaps)
    plan = {'format': 'topclass-expert-education-v2', 'brief': brief,
            'agent': {'id': agent['id'], 'name': agent['name']}, 'snapshot': copy.deepcopy(library.snapshot),
            'status': 'AI-selected education' if selected else 'Awaiting AI course choices',
            'selection_limits': 'The AI selected these courses and priorities from the complete saved usable catalog. No code ranking or automatic shortlist. Catalog coverage remains incomplete.',
            'selection_author': 'host-agent', 'courses': selected,
            'must_have_courses': [row for row in selected if row['selection_role'] == 'must-have'],
            'supplemental_courses': [row for row in selected if row['selection_role'] == 'supplemental'],
            'requirement_coverage': coverage, 'gaps': gaps, 'materials_to_supply': books, 'unidentified_materials': unresolved,
            'unchecked_constraints': brief.get('constraints', []), 'catalog_coverage': 'saved book-backed courses only',
            'unusable_course_research_gaps': [], 'enrichment_backlog': [],
            'not_claimed': ['complete course coverage', 'books have been read', 'professional competence'],
            'handoff': {'next_step': 'Review the selected courses and their book list',
                        'supplied': False, 'processed': False}}
    return {'brief': brief, 'proposal': proposal, 'plan': plan, 'candidates': {'courses': alternatives}}
