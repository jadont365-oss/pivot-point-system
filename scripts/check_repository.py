"""Audit Git's staged source tree without exposing credential content."""
import hashlib
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = tuple(re.compile(p) for p in (
    r"td1_[A-Za-z0-9_]{20,}",
    r"gh[pousr]_[A-Za-z0-9_]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
))


def sensitive(content):
    return any(pattern.search(content) for pattern in PATTERNS)


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def audit():
    paths = git('ls-files', '-z').decode('utf-8').split('\0')[:-1]
    ignore = git('show', ':.gitignore').decode('utf-8')
    allowed = {line[2:] for line in ignore.splitlines() if line.startswith('!/') and not line.endswith('/')}
    manifest = json.loads(git('show', ':SOURCE_MANIFEST.json'))['files']
    if not paths or not manifest or not set(manifest) <= set(paths):
        raise ValueError('Empty or incomplete staged source tree')
    for name in paths:
        if name not in allowed:
            raise ValueError('Non-allowlisted tracked path: ' + name)
        payload = git('show', ':' + name)
        if len(payload) > 512_000:
            raise ValueError('Oversized tracked file: ' + name)
        if sensitive(payload.decode('utf-8')):
            raise ValueError('Potential credential in tracked file: ' + name)
        if name in manifest and hashlib.sha256(payload).hexdigest() != manifest[name]:
            raise ValueError('Original source binding changed: ' + name)
    print(f'Repository audit passed: {len(paths)} tracked files; {len(manifest)} original source hashes exact')


if __name__ == '__main__':
    audit()
