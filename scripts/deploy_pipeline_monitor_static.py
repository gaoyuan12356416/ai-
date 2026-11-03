#!/usr/bin/env python3
"""Deploy exact GitHub monitor assets with a previous-version guard.

No API/service restart or publication-state mutation. Default is read-only.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sha', required=True)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not all(re.fullmatch('[0-9a-f]{40}', s) for s in (args.sha, args.expected_sha)):
        raise ValueError('exact_github_commits_required')
    repo = Path('/mnt/data-disk/pipeline-dispatch/github')
    env = dict(os.environ, GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes')
    for sha in (args.sha, args.expected_sha):
        subprocess.run(['git', '-C', str(repo), 'fetch', 'origin', sha], env=env,
                       check=True, capture_output=True, timeout=120)
    names = ('pipeline-monitor.js', 'pipeline-monitor.css', 'pipeline-monitor.html')
    roots = {'main': Path('/root/drama_material_service/static'), 'nginx': Path('/usr/share/nginx/html')}
    def content(sha, name):
        return subprocess.check_output(['git', '-C', str(repo), 'show', sha + ':static/' + name])
    digest = lambda data: hashlib.sha256(data).hexdigest()
    replacement = {name: content(args.sha, name) for name in names}
    for name in names:
        expected = content(args.expected_sha, name)
        for root in roots.values():
            if (root / name).read_bytes() != expected:
                raise ValueError('unexpected_live_static_version')
    report = {'sha': args.sha, 'previous_sha': args.expected_sha, 'apply': args.apply, 'restarted': []}
    if not args.apply:
        print(json.dumps(report)); return
    if not os.path.ismount('/mnt/data-disk'): raise ValueError('data_mount_required')
    backup = Path('/mnt/data-disk/pipeline-dispatch') / ('monitor-static-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700, exist_ok=False)
    report.update(backup=str(backup), files=[])
    for label, root in roots.items():
        (backup / label).mkdir()
        # Dependencies first; HTML then changes the browser cache keys.
        for name in names:
            target = root / name
            (backup / label / name).write_bytes(target.read_bytes())
            temp = root / (name + '.pipeline-deploy-tmp')
            with temp.open('xb') as handle:
                handle.write(replacement[name]); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp, target.stat().st_mode)
            os.replace(temp, target)
            report['files'].append({'path': str(target), 'sha256': digest(target.read_bytes())})
    for name in names:
        req = urllib.request.Request('https://ai.yingliangads.com/' + name + '?verify=' + args.sha,
                                     headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200 or response.read() != replacement[name]:
                raise ValueError('public_static_mismatch')
    report['public_verified'] = True
    (backup / 'deploy.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__': main()
