"""Patch the two current X runtime composites, preserving unrelated releases.

Rollback stops *new* allocations but retains the shared reader, all mappings,
files and routes. Never restore a historical publication/allocator database.
"""
import argparse
import ast
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path('/mnt/data-disk/x-shared-short-links')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
LANES = {
    'standard': ('/opt/x-post-automation/current', 'x-post-automation.service', 8810,
                 '/var/lib/x-post-automation/accounts.sqlite3'),
    'tag': ('/opt/x-tag-publisher/current', 'x-tag-post.service', 8821,
            '/mnt/data-disk/x-tag-publisher/posts.sqlite3'),
}
NGINX = Path('/etc/nginx/conf.d/gy.g2flow.com.conf')
INCLUDE = '    include /etc/nginx/snippets/x-shared-short-links.conf;\n'
SNIPPET = '''location ~ ^/s/([1-9][0-9]*)$ {
    alias /mnt/data-disk/x-shared-short-links/public/$1.html;
    default_type text/html;
    autoindex off;
    limit_except GET { deny all; }
    add_header Cache-Control "no-store" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header X-Content-Type-Options nosniff always;
}
'''


def run(*args, timeout=45, check=True):
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError('command_failed:' + str(args[0]) + ':' + r.stderr[-1500:])
    return r.stdout.strip()


def sha(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def atomic(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.short-next')
    temp.write_bytes(data)
    temp.chmod(mode)
    temp.replace(path)


def save(folder, info):
    atomic(folder / 'manifest.json', (json.dumps(info, indent=2) + '\n').encode(), 0o600)


def once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError('source_anchor_changed:' + old[:100])
    return text.replace(old, new, 1)


def patch_service(text):
    text = once(text, '    return "%s/%s.html" % (base, log_id)',
                '    from features.x_posts.shared_short_links import allocate\n'
                '    allocated = allocate(base, log_id)\n'
                '    if allocated is not None:\n'
                '        return allocated\n'
                '    return "%s/%s.html" % (base, log_id)')
    signature = 'def write_short_redirect(public_root, log_id, long_url, *, durable_storage=None):\n'
    text = once(text, signature, signature +
                '    from features.x_posts.shared_short_links import write_allocated\n'
                '    target = _validate_w2a_url(long_url)\n'
                '    allocated = write_allocated(DEFAULT_SHORT_BASE_URL, log_id, target, _write_short_redirect_file)\n'
                '    if allocated is not None:\n'
                '        return allocated\n'
                '    return _write_short_redirect_file(public_root, log_id, target, durable_storage=durable_storage)\n\n\n'
                'def _write_short_redirect_file(public_root, log_id, long_url, *, durable_storage=None):\n')
    compile(text, 'service.py', 'exec')
    return text


def patch_native(text):
    # A shared URL must exist durably before a frozen log can be returned by
    # the early-return path on a subsequent canonical request.
    old = '        log = self.store.prepare_log(log["id"], long_url, short, body)\n'
    tree = ast.parse(text)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_links')
    lines = text.splitlines(keepends=True)
    body = ''.join(lines[node.lineno-1:node.end_lineno])
    changed = once(body, old, '')
    final = changed.rfind('\n        return log')
    if final < 0:
        raise RuntimeError('native_final_return_missing')
    changed = changed[:final+1] + old + changed[final+1:]
    text = text[:sum(map(len, lines[:node.lineno-1]))] + changed + ''.join(lines[node.end_lineno:])
    compile(text, 'x_native.py', 'exec')
    if changed.index('posts.write_short_redirect(') > changed.index('self.store.prepare_log('):
        raise RuntimeError('native_link_order_invalid')
    return text


def stage(folder):
    assert run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git', '-C', SOURCE, 'status', '--porcelain'), 'dirty_source'
    commit = run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit), 'source_not_pushed'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    info = dict(commit=commit, source=str(SOURCE), status='staging', lanes={})
    for lane, (link, unit, port, db) in LANES.items():
        previous = Path(link).resolve()
        candidate = folder / lane
        shutil.copytree(previous, candidate, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
        # Keep the owners and modes of unrelated runtime resources exactly.
        for target in [candidate, *candidate.rglob('*')]:
            origin = previous / target.relative_to(candidate)
            if origin.exists() and not target.is_symlink():
                stat = origin.stat()
                os.chown(target, stat.st_uid, stat.st_gid)
        files = {}
        for name, transform in [('features/x_posts/service.py', patch_service),
                                ('features/pipeline_dispatch/x_native.py', patch_native)]:
            path = candidate / name
            before = sha(path)
            atomic(path, transform(path.read_text()).encode())
            files[name] = dict(before=before, after=sha(path))
        name = 'features/x_posts/shared_short_links.py'
        assert not (candidate / name).exists(), 'allocator_already_present'
        atomic(candidate / name, (SOURCE / name).read_bytes())
        files[name] = dict(before=None, after=sha(candidate / name))
        for name in ['test_x_shared_short_links.py', 'test_x_shared_short_deployment.py']:
            shutil.copy2(SOURCE / 'scripts' / name, candidate / 'scripts' / name)
        shutil.copy2(SOURCE / 'scripts/deploy_x_shared_short_links.py', candidate / 'scripts/deploy_x_shared_short_links.py')
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest',
                                 'scripts.test_x_shared_short_links', 'scripts.test_x_shared_short_deployment', '-q'],
                                cwd=candidate, capture_output=True, text=True, timeout=90)
        (folder / (lane + '-tests.txt')).write_text(result.stdout + result.stderr)
        assert result.returncode == 0, lane + '_tests_failed'
        with closing(sqlite3.connect(Path(db).as_uri() + '?mode=ro', uri=True)) as src, \
                closing(sqlite3.connect(folder / (lane + '-ledger-before.sqlite3'))) as dst:
            src.backup(dst)
        (folder / (lane + '-ledger-before.sqlite3')).chmod(0o600)
        info['lanes'][lane] = dict(previous=str(previous), candidate=str(candidate), files=files)
    assert 'location ~ ^/s/' not in NGINX.read_text(), 'shared_route_exists'
    shutil.copy2(NGINX, folder / 'nginx-before.conf')
    info['nginx_before'] = sha(NGINX)
    info['status'] = 'staged'
    save(folder, info)
    print(json.dumps(dict(status='staged', commit=commit, folder=str(folder))), flush=True)


def unit_state(unit):
    return run('systemctl', 'show', unit, '-p', 'ActiveState', '--value')


def health(port):
    deadline = time.monotonic() + 20
    while True:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=2) as r:
                assert r.status == 200
                return
        except (OSError, AssertionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(.25)


def switch_unit(unit, port, callback):
    """Drain only this sidecar's HTTP requests; nodes keep their live leases."""
    pid = run('systemctl', 'show', unit, '-p', 'MainPID', '--value')
    assert pid != '0', 'publisher_not_running:' + unit
    marker = 'x-short-' + str(port) + '-' + datetime.now(timezone.utc).strftime('%H%M%S')
    rule = ['-p', 'tcp', '--dport', str(port), '-m', 'conntrack', '--ctstate', 'NEW',
            '-m', 'comment', '--comment', marker, '-j', 'DROP']
    watchdog = marker + '-gate'
    run('systemd-run', '--quiet', '--unit=' + watchdog, '--on-active=75s', '--', '/usr/sbin/iptables', '-D', 'INPUT', *rule)
    gated = False
    try:
        run('iptables', '-I', 'INPUT', '1', *rule)
        gated = True
        deadline = time.monotonic() + 30
        while True:
            sockets = [l for l in run('ss', '-Hntp').splitlines() if ('pid=' + pid + ',') in l]
            listeners = [l for l in run('ss', '-Hlntp').splitlines() if ('pid=' + pid + ',') in l]
            if not sockets and not any(int(l.split()[1]) for l in listeners):
                break
            assert time.monotonic() < deadline, 'active_requests_preserved_retry_later:' + unit
            time.sleep(.25)
        callback()
        run('systemctl', 'daemon-reload')
        run('systemctl', 'stop', unit, timeout=45)
        # ExecStartPost uses the same HTTP port for its readiness probe. Release
        # the gate after the drained old listener exits, before starting it.
        run('iptables', '-D', 'INPUT', *rule)
        gated = False
        run('systemctl', 'start', unit, timeout=45)
    finally:
        if gated:
            run('iptables', '-D', 'INPUT', *rule, check=False)
        run('systemctl', 'stop', watchdog + '.timer', check=False)
    health(port)


def activate(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    assert info['status'] in ('staged', 'active', 'allocation_disabled', 'applying'), 'not_staged'
    run(sys.executable, HEALTH, 'snapshot', '--lane', 'x', '--output', folder / 'schedulers-before.json')
    if not rollback:
        for lane, (link, _, _, _) in LANES.items():
            row = info['lanes'][lane]
            assert str(Path(link).resolve()) in (row['previous'], row['candidate']), 'concurrent_release:' + lane
        if info['status'] == 'staged':
            assert sha(NGINX) == info['nginx_before'], 'nginx_changed'
        sys.path.insert(0, str(SOURCE))
        from features.x_posts.shared_short_links import initialize
        import pwd
        initialize(ROOT)
        owner = pwd.getpwnam('x-post-automation')
        for path, mode in [(ROOT,0o755), (ROOT/'public',0o755), (ROOT/'links.sqlite3',0o600)]:
            os.chown(path, owner.pw_uid, owner.pw_gid)
            path.chmod(mode)
        atomic('/etc/nginx/snippets/x-shared-short-links.conf', SNIPPET.encode())
        config = NGINX.read_text()
        if INCLUDE not in config:
            anchor = '    include /etc/nginx/snippets/x-tag-post-s2l.conf;\n'
            atomic(NGINX, once(config, anchor, anchor + INCLUDE).encode())
        run('nginx', '-t')
        run('systemctl', 'reload', 'nginx')
    info['status'] = 'applying'
    save(folder, info)
    try:
        for lane, (link, unit, port, _) in LANES.items():
            row = info['lanes'][lane]
            current = Path(link)
            assert str(current.resolve()) in (row['previous'], row['candidate']), 'concurrent_release:' + lane
            # A controller may exit after a successful switch. Reconcile the
            # running process before considering another restart.
            pid = run('systemctl', 'show', unit, '-p', 'MainPID', '--value')
            expected_flag = '0' if rollback else '1'
            if pid != '0' and str(current.resolve()) == row['candidate']:
                env = dict(v.split('=', 1) for v in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in v)
                if (os.readlink('/proc/' + pid + '/cwd') == row['candidate']
                        and env.get('X_POST_SHARED_SHORT_ROOT') == str(ROOT)
                        and env.get('X_POST_SHARED_SHORT_ALLOCATE') == expected_flag):
                    health(port)
                    row['activated'] = True
                    save(folder, info)
                    print(json.dumps(dict(lane=lane, status='already_healthy', rollback=rollback)), flush=True)
                    continue
            if rollback:
                assert str(current.resolve()) == row['candidate'], 'rollback_release_changed'
            else:
                for name, hashes in row['files'].items():
                    assert sha(Path(row['candidate']) / name) == hashes['after'], 'candidate_changed:' + name
                    expected = hashes['after'] if str(current.resolve()) == row['candidate'] else hashes['before']
                    assert sha(current / name) == expected, 'live_file_changed:' + name
            def apply():
                override = Path('/etc/systemd/system') / (unit + '.d/98-shared-short-links.conf')
                data = ('[Unit]\nRequiresMountsFor=' + str(ROOT) + '\n[Service]\n'
                        'Environment=X_POST_SHARED_SHORT_ROOT=' + str(ROOT) + '\n'
                        'Environment=X_POST_SHARED_SHORT_ALLOCATE=' + ('0' if rollback else '1') + '\n'
                        'ReadWritePaths=' + str(ROOT) + '\n')
                if not override.exists() and not rollback:
                    row['override_before'] = None
                atomic(override, data.encode())
                temp = current.with_name('current.shared-short-next')
                temp.symlink_to(row['candidate'])
                temp.replace(current)
            switch_unit(unit, port, apply)
            row['activated'] = True
            save(folder, info)
            print(json.dumps(dict(lane=lane, status='healthy', rollback=rollback)), flush=True)
    finally:
        run(sys.executable, HEALTH, 'restore', '--baseline', folder / 'schedulers-before.json',
            '--wait-seconds', '25', '--output', folder / 'schedulers-after.json', timeout=45)
    info['status'] = 'allocation_disabled' if rollback else 'active'
    info['controller_commit'] = run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    save(folder, info)
    print(json.dumps(dict(status=info['status'], commit=info['commit'])), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['stage', 'activate', 'rollback'])
    parser.add_argument('--folder', required=True)
    args = parser.parse_args()
    folder = Path(args.folder).resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    if args.action == 'stage':
        stage(folder)
    else:
        activate(folder, args.action == 'rollback')
