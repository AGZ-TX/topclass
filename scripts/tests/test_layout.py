import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.system import skill_content
from course_discovery import CatalogDiscovery
from distribution import export


ROOT = Path(__file__).resolve().parents[2]


class LayoutTest(unittest.TestCase):
    def test_export_keeps_five_items_and_launches_from_another_directory(self):
        catalog = CatalogDiscovery.from_records(
            [{'course_key': 'design', 'title': 'Engineering Design'}],
            [{'course_key': 'design', 'status': 'found', 'materials': [
                {'title': 'Design Reference', 'resource_type': 'book'}]}])
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / 'public copy with spaces'
            export(destination, root=ROOT, catalog=catalog)
            self.assertEqual({p.name for p in destination.iterdir()},
                             {'data', 'docs', 'scripts', 'README.md', '.gitignore'})
            self.assertTrue((destination / 'docs/LICENSE').is_file())
            self.assertIn('docs/AGENTS.md', (destination / 'README.md').read_text())
            launcher = destination / 'scripts/topclass'
            for arguments in [['--help'], ['setup', '--help'], ['hire']]:
                result = subprocess.run([sys.executable, launcher, *arguments], cwd=temp,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, launcher, '--home', Path(temp) / 'agents',
                                     'skills', '--host', 'codex', '--destination', Path(temp) / 'host'],
                                    cwd=temp, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ['hire', 'recall', 'add']:
                installed = Path(temp) / 'host/.agents/skills' / name / 'SKILL.md'
                self.assertIn(str(destination) + '/scripts/topclass', installed.read_text())
                self.assertNotIn('__TOPCLASS_ROOT__', installed.read_text())
            subprocess.run(['git', 'init', '-q', destination], check=True)
            attributes = subprocess.check_output(['git', '-C', destination, 'check-attr', 'text', 'eol',
                '--', 'data/public/catalog.json', 'docs/LICENSE', 'scripts/topclass.cmd'], text=True)
            self.assertIn('data/public/catalog.json: text: unset', attributes)
            self.assertIn('docs/LICENSE: text: unset', attributes)
            self.assertIn('scripts/topclass.cmd: eol: crlf', attributes)

    def test_windows_skill_and_launcher_use_relocated_entry_point(self):
        for name in ['hire', 'recall', 'add']:
            text = skill_content(ROOT, name, platform='nt')
            self.assertIn('& "' + ROOT.as_posix() + '/scripts/topclass.cmd"', text)
            self.assertNotIn('"' + ROOT.as_posix() + '/topclass"', text)
        launcher = (ROOT / 'scripts/topclass.cmd').read_bytes()
        self.assertIn(b'\r\n', launcher)
        self.assertNotIn(b'\\r\\n', launcher)
        self.assertIn(b'"%~dp0topclass"', launcher)


if __name__ == '__main__':
    unittest.main()
