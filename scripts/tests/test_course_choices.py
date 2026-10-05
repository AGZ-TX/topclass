import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.app import hire, revise, check_html
from core.planner import apply_selection
from core.presentation import TEMPLATE
from course_discovery import CatalogDiscovery


class CourseChoicesTest(unittest.TestCase):
    def test_fresh_hire_has_ten_flat_suggestions_and_membership_only_choices(self):
        rows = [{'course_key': str(i), 'title': 'Engineering Topic ' + str(i)} for i in range(11)]
        materials = [{'course_key': row['course_key'], 'status': 'found', 'materials': [
            {'title': row['title'] + ' Reference', 'resource_type': 'book'}]} for row in rows]
        catalog = CatalogDiscovery.from_records(rows, materials)
        purpose = 'Support engineering design.'
        brief = {'original_description': purpose, 'role': 'Design Engineer', 'capabilities': [
            {'id': 'design', 'description': 'engineering design', 'priority': 'essential'}]}
        proposal = {'courses': [{'course_id': 'course:' + row['course_key'], 'reviews': [{
            'course_id': 'course:' + row['course_key'], 'requirement': 'engineering design',
            'decision': 'essential', 'field': 'title', 'quote': row['title'],
            'rationale': 'Synthetic catalog evidence for the fresh-hire contract test.'}]} for row in rows[:10]]}
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'agents'
            agent = hire(home, purpose, brief=brief, catalog=catalog)['agent_id']
            result = revise(home, agent, proposal=proposal)
            data = json.loads(Path(result['planner_data']).read_text())
            self.assertEqual(len(data['recommendation_courses']), 10)
            self.assertEqual(len(data['manifest']['courses']), 10)
            profile = {'title': 'Design Engineer', 'description': 'Supports engineering design.', 'shortfall': ''}
            html = TEMPLATE.read_text().replace('__PLANNER_DATA__', json.dumps(data)).replace('__AGENT_PROFILE__', json.dumps(profile))
            Path(result['education_html']).write_text(html)
            self.assertTrue(check_html(home, agent)['template_matches'])
            self.assertNotIn('course-priority', html)
            folder = Path(result['planner_data']).parent
            plan = json.loads((folder / 'plan.json').read_text())
            candidates = json.loads((folder / 'candidates.json').read_text())
            flat = data['manifest'] | {'courses': [{'course_id': row['course_id']} for row in data['manifest']['courses']]}
            flat['courses'].pop(0)
            selected = apply_selection(plan, flat, candidates)
            self.assertEqual(len(selected['courses']), 9)
            self.assertNotIn('course:0', [row['course']['id'] for row in selected['courses']])
            flat['courses'].append({'course_id': 'course:0'})
            self.assertEqual(len(apply_selection(plan, flat, candidates)['courses']), 10)
            flat['courses'].append({'course_id': 'course:0'})
            with self.assertRaises(ValueError):
                apply_selection(plan, flat, candidates)


if __name__ == '__main__':
    unittest.main()
