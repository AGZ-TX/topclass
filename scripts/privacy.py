import argparse
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'google-credential': re.compile(rb'AIza[0-9A-Za-z_-]{35}|AQ\.[0-9A-Za-z_-]{20,}'),
    'github-credential': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'private-key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}
PRIVATE_NAMES = {'.google-api-key', '.google-keys', '.google.json', '.search.json', 'google-api-key', 'google-profile.json'}


def private_path(name):
    path = Path(name)
    return (path.name in PRIVATE_NAMES or path.suffix in {'.db', '.sqlite', '.sqlite3', '.pem', '.key', '.log'}
            or path.name == '.env' or (path.name.startswith('.env.') and path.name != '.env.example')
            or any(part in {'derived_private', 'private_sources', 'node_modules', '.venv'} for part in path.parts)
            or name.startswith(('.agents/skills/', '.claude/skills/')))


def inspect(content, name, findings, revision=None):
    location = {'path': name}
    if revision:
        location['object'] = revision
    if private_path(name):
        findings.append(location | {'kind': 'private-file'})
    for kind, pattern in PATTERNS.items():
        if pattern.search(content):
            findings.append(location | {'kind': kind})


def audit(root=ROOT, history=False):
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root)
    findings, files, blobs = [], 0, 0
    for name in dict.fromkeys(filter(None, names.decode().split('\0'))):
        path = root / name
        if path.is_symlink():
            findings.append({'path': name, 'kind': 'symlink'})
        elif path.is_file():
            inspect(path.read_bytes(), name, findings)
            files += 1
    if history:
        objects = subprocess.check_output(['git', 'rev-list', '--objects', '--all'], cwd=root).splitlines()
        with subprocess.Popen(['git', 'cat-file', '--batch'], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
            for entry in objects:
                revision, _, name = entry.partition(b' ')
                process.stdin.write(revision + b'\n')
                process.stdin.flush()
                header = process.stdout.readline().split()
                content = process.stdout.read(int(header[2]))
                process.stdout.read(1)
                if header[1] == b'blob':
                    inspect(content, name.decode(errors='replace'), findings, revision.decode())
                    blobs += 1
            process.stdin.close()
    return {'files_scanned': files, 'history_blobs_scanned': blobs, 'findings': findings,
            'scope': 'Working tree and locally available reachable Git history; pattern checks do not prove absence of every secret.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manually inspect public files without printing credential values.')
    parser.add_argument('--history', action='store_true', help='Also inspect locally reachable historical blobs')
    args = parser.parse_args(argv)
    result = audit(history=args.history)
    print(json.dumps(result, indent=2))
    return 1 if result['findings'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
