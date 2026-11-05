"""Scoped GitHub-first Portuguese language fix, with code-only rollback."""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import urllib.request

import deploy_x_pool_local_failures as base

SOURCE = Path(__file__).resolve().parents[1]
BASELINE = {
    'features/x_accounts/language.py': '2f69e5ec4294ebcc2268607b0c60bb47fdc7dd582cb8f54cc447825cc175f84f',
    'features/x_video_publish/core.py': 'a2f7ec2977291df4570984803224a049f40f0899cbe7425c6efa8275d1775ae4',
    'features/x_video_publish/sources.py': '0081c33b0336ea072055591b10732d457fa3c76e2f2c0f47e8e3f8333ffb3189',
    'static/x-video-publish.js': '13d48be4fdabc6bfa5aafc70075ddb38100fbd6581375ff6d77a61e2ff503c27',
    'static/x-video-publish.html': '8f6075988e97cdb16f50a6411d9ba7e516944d02a90bcd661c0dffb707720ce3',
}


def prepare():
    assert base.run('findmnt', '-no', 'UUID', '/mnt/data-disk') == base.UUID
    assert shutil.disk_usage(base.ROOT).free > 2 * 1024**3
    assert not base.run('git', '-C', str(SOURCE), 'status', '--porcelain')
    commit = base.run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    assert base.run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit)
    folder = base.ROOT / 'backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-pt-' + commit[:8])
    folder.mkdir(mode=0o700)
    items = []
    for relative, expected in BASELINE.items():
        targets = [base.MAIN / relative, base.CURRENT.resolve() / relative]
        if relative.startswith('static/'):
            targets.append(Path('/usr/share/nginx/html') / Path(relative).name)
        for target in targets:
            assert base.digest(target) == expected, 'concurrent change: ' + str(target)
            saved = folder / ('before-' + str(len(items)))
            shutil.copy2(target, saved)
            items.append({'path': str(target), 'before': str(saved), 'after': str(SOURCE / relative),
                'before_sha': expected, 'after_sha': base.digest(SOURCE / relative)})
        if relative.endswith('.py'):
            compile((SOURCE / relative).read_bytes(), relative, 'exec')
    m = {'phase': 'prepared', 'commit': commit, 'source': str(SOURCE),
         'runtime': str(base.CURRENT.resolve()), 'items': items}
    base.save(folder, m)
    return {'phase': m['phase'], 'folder': str(folder), 'commit': commit}


def activate(folder, m):
    assert m['phase'] == 'draining'
    observed = base.active()
    if any(observed.values()):
        return {'phase': 'waiting', **observed}
    rollback = m.get('rollback', False)
    before, after = ('after', 'before') if rollback else ('before', 'after')
    assert str(base.CURRENT.resolve()) == m['runtime'], 'runtime changed; restage'
    for item in m['items']:
        assert base.digest(item['path']) == item[before + '_sha'], 'concurrent file change'
    rules, changed = [], []
    main_active = base.state('drama-material-api.service') == 'active'
    stopped = False
    try:
        for port in (8787, 8810, 18833):
            rule = ['OUTPUT', '-d', '127.0.0.1', '-p', 'tcp', '--dport', str(port), '-m', 'conntrack',
                '--ctstate', 'NEW', '-m', 'comment', '--comment', 'x-pt-' + m['commit'][:8], '-j', 'REJECT', '--reject-with', 'tcp-reset']
            base.run('iptables', '-w', '5', '-I', *rule)
            rules.append(rule)
        deadline = time.monotonic() + 20
        while any(base.run('ss', '-Htn', 'state', 'established', 'sport', '=', ':' + str(p)) for p in (8787, 8810, 18833)):
            if time.monotonic() > deadline:
                return {'phase': 'waiting_http'}
            time.sleep(.25)
        assert not base.active()['running_x_jobs']
        suffix = 'rollback' if rollback else 'deploy'
        with contextlib.closing(base.readonly(base.DB)) as src, contextlib.closing(sqlite3.connect(str(folder / ('accounts-' + suffix + '.sqlite3')))) as dst:
            src.backup(dst)
        token_dir = Path('/var/lib/x-post-automation/tokens')
        shutil.copytree(token_dir, folder / ('tokens-' + suffix))
        token_hashes = {p.name: base.digest(p) for p in token_dir.glob('*.json')}
        stopped = True
        base.run('systemctl', 'stop', 'x-post-automation.service')
        if main_active:
            base.run('systemctl', 'stop', 'drama-material-api.service')
        for item in m['items']:
            base.copy(Path(item[after]), Path(item['path']))
            changed.append(item)
        for rule in rules[:]:
            base.run('iptables', '-w', '5', '-D', *rule)
            rules.remove(rule)
        if main_active:
            base.run('systemctl', 'start', 'drama-material-api.service')
        base.run('systemctl', 'start', 'x-post-automation.service')
        base.ready(8810)
        if m['units']['x-auto-post-service.service'] == 'active':
            base.run('systemctl', 'start', 'x-auto-post-service.service')
            base.ready(18833)
        if main_active:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/ui/topbar', timeout=15) as response:
                assert response.status == 200
        assert all(base.digest(i['path']) == i[after + '_sha'] for i in m['items'])
        m['tokens_unchanged'] = token_hashes == {p.name: base.digest(p) for p in token_dir.glob('*.json')}
        assert m['tokens_unchanged']
        m['phase'] = 'rolled_back' if rollback else 'complete'
        base.save(folder, m)
    except BaseException:
        if changed:
            base.run('systemctl', 'stop', 'x-post-automation.service')
            if main_active:
                base.run('systemctl', 'stop', 'drama-material-api.service')
        for item in reversed(changed):
            base.copy(Path(item[before]), Path(item['path']))
        m['phase'] = 'failed_restored'
        base.save(folder, m)
        raise
    finally:
        for rule in rules:
            base.run('iptables', '-w', '5', '-D', *rule)
        if stopped:
            if main_active:
                base.run('systemctl', 'start', 'drama-material-api.service')
            base.run('systemctl', 'start', 'x-post-automation.service')
            if m['units']['x-auto-post-service.service'] == 'active':
                base.run('systemctl', 'start', 'x-auto-post-service.service')
        if m['phase'] != 'draining':
            base.resume(folder, m)
    return {'phase': m['phase'], 'folder': str(folder), 'commit': m['commit'], 'tokens_unchanged': m['tokens_unchanged']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('prepare', 'drain', 'switch', 'rollback', 'abort'), required=True)
    parser.add_argument('--folder', type=Path)
    args = parser.parse_args()
    if args.phase == 'prepare':
        result = prepare()
    else:
        folder = args.folder.resolve()
        assert folder.parent == base.ROOT / 'backups' and '-pt-' in folder.name
        m = json.loads((folder / 'manifest.json').read_text())
        if args.phase in ('drain', 'rollback'):
            result = base.drain(folder, m, args.phase == 'rollback')
        elif args.phase == 'switch':
            result = activate(folder, m)
        else:
            assert m['phase'] == 'draining'
            base.resume(folder, m)
            m['phase'] = 'complete' if m.get('rollback') else 'prepared'
            base.save(folder, m)
            result = {'phase': m['phase'], 'admissions_restored': True}
    print(json.dumps(result), flush=True)
