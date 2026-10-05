import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

from core.paths import default_home, private_home
from core.system import skill_content, venv_python


ROOT = Path(__file__).resolve().parents[1]


def run(command, directory=ROOT):
    subprocess.run(command, cwd=directory, check=True)


def skills(host, destination):
    from core.app import install_skills

    directory = destination / ('.agents' if host == 'codex' else '.claude') / 'skills'
    paths = [directory / name / 'SKILL.md' for name in ('hire', 'recall', 'add')]
    if all(path.is_file() and not path.is_symlink() and path.read_text() ==
           skill_content(ROOT, path.parent.name)
           for path in paths):
        return {'status': 'already-installed', 'host': host}
    return install_skills(destination, host)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare Topclass on your device without searching books or making model calls.')
    parser.add_argument('--host', choices=('codex', 'claude'), default='codex')
    parser.add_argument('--destination', type=Path, default=ROOT)
    parser.add_argument('--home', type=Path, default=default_home(ROOT))
    parser.add_argument('--search-repo', type=Path)
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--skip-google', action='store_true', help='Choose courses without setting up embeddings yet')
    parser.add_argument('--skip-browser', action='store_true', help='Prepare the finder without downloading Chromium yet')
    args = parser.parse_args(argv)
    args.home = private_home(ROOT, args.home)
    args.destination = args.destination.expanduser().absolute()
    if args.key_file:
        args.key_file = args.key_file.expanduser().absolute()
    if args.search_repo:
        args.search_repo = args.search_repo.expanduser().absolute()
    finder = args.search_repo or ROOT / 'scripts' / 'finder'
    node, npm = shutil.which('node'), shutil.which('npm')
    if not node or not npm:
        raise ValueError('Install Node.js 20+ with npm, then run setup again.')
    version = subprocess.check_output([node, '--version'], text=True).strip()
    if int(version.removeprefix('v').split('.')[0]) < 20:
        raise ValueError('Topclass book search needs Node.js 20+.')
    if not (finder / 'package-lock.json').is_file() or not (finder / 'search.py').is_file():
        raise ValueError('The book finder source or its lockfile is missing.')
    environment = ROOT / '.venv'
    if environment.is_symlink():
        raise ValueError('The project virtual environment cannot be a symlink.')
    python = venv_python(ROOT)
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')])
    run([npm, 'ci', '--ignore-scripts'], finder)
    run([npm, 'run', 'build'], finder)
    if not args.skip_browser:
        run([node, 'node_modules/playwright/cli.js', 'install', 'chromium'], finder)
    configured = subprocess.run([str(python), str(ROOT / 'topclass'), '--home', str(args.home),
                                'finder', '--repo', str(finder)], check=True, capture_output=True, text=True)
    result = {'finder': json.loads(configured.stdout), 'skills': skills(args.host, args.destination),
              'google': 'not-configured', 'next': 'Use /hire and describe your agent’s purpose.'}
    if not args.skip_google:
        if (args.home / '.google.json').is_file() and not args.key_file and not os.environ.get('GEMINI_API_KEY'):
            result['google'] = 'already-configured'
        elif args.key_file or os.environ.get('GEMINI_API_KEY') or sys.stdin.isatty():
            from core.pipeline import configure_google
            result['google'] = configure_google(args.home, args.key_file)
        else:
            result['next'] = 'Run ./topclass google once, then use /hire.'
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
