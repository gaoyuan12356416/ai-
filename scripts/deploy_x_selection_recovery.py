#!/usr/bin/env python3
"""Scoped install after the tag control API has stopped gracefully.

Source must be a fetched GitHub release. No queue rows or receipt files change.
The capacity gateway GET method and HTML script hook are patched in place so
concurrent capacity-setting changes are retained. Rollback preserves all ledgers.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def method(source, cls, name):
    tree = ast.parse(source)
    return next(f for c in tree.body if isinstance(c, ast.ClassDef) and c.name == cls
                for f in c.body if isinstance(f, ast.FunctionDef) and f.name == name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    source, backup = args.source.resolve(), args.backup.resolve()
    assert str(backup).startswith('/mnt/data-disk/x-selection-recovery-20261118/')
    assert subprocess.check_output(['systemctl', 'show', 'x-tag-auto.service', '-p', 'MainPID', '--value'], text=True).strip() == '0'
    assert subprocess.check_output(['systemctl', 'show', 'x-tag-auto-runner.service', '-p', 'MainPID', '--value'], text=True).strip() == '0'
    tag = Path('/opt/x-tag-publisher/current').resolve()
    gateway = Path('/opt/pipeline-capacity-gateway/current').resolve()
    expected = {'core.py': '684c8556426e4bbed443ca51e2466597871ce91e27e14bb67116d6706ab328a5',
                'publisher.py': 'e835da1a464c14f78f71ed6b034c136599a076fe7c143de7b3c9cd716a254c39'}
    for name, sha in expected.items():
        assert digest(tag / 'features/x_auto_posts' / name) == sha, 'tag_runtime_changed:' + name
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {'source': str(source), 'tag': str(tag), 'gateway': str(gateway), 'files': []}

    def install(path, data, mode=0o644):
        path = Path(path)
        item = {'path': str(path), 'existed': path.exists()}
        if path.exists():
            item['backup'] = str(backup / str(len(manifest['files'])))
            shutil.copy2(path, item['backup'])
            item['before'] = digest(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.selection-new')
        temporary.write_bytes(data)
        temporary.chmod(mode)
        if path.exists():
            st = path.stat()
            os.chown(temporary, st.st_uid, st.st_gid)
        temporary.replace(path)
        item['after'] = digest(path)
        manifest['files'].append(item)
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))

    for name in ('selection_recovery.py', 'core.py', 'publisher.py'):
        relative = Path('features/x_auto_posts') / name
        install(tag / relative, (source / relative).read_bytes())
    for name in ('drain_diagnostics.py', 'maintenance.py'):
        relative = Path('features/pipeline_dispatch') / name
        if name == 'maintenance.py' and (gateway / relative).exists():
            continue
        install(gateway / relative, (source / relative).read_bytes())
    relative = Path('features/pipeline_dispatch/capacity_gateway.py')
    current = (gateway / relative).read_text()
    replacement = (source / relative).read_text()
    old, new = method(current, 'Handler', 'do_GET'), method(replacement, 'Handler', 'do_GET')
    old_lines, new_lines = current.splitlines(True), replacement.splitlines(True)
    assert 'drain-diagnostics' not in ''.join(old_lines[old.lineno - 1:old.end_lineno])
    assert 'self.path == "/health"' in ''.join(old_lines[old.lineno - 1:old.end_lineno])
    old_lines[old.lineno - 1:old.end_lineno] = new_lines[new.lineno - 1:new.end_lineno]
    install(gateway / relative, ''.join(old_lines).encode())
    # Existing admin Cookie authentication is reused by this exact route.
    install('/etc/nginx/default.d/pipeline-drain-diagnostics.conf', b'''location = /api/pipeline-monitor/drain-diagnostics {
    proxy_pass http://127.0.0.1:18960;
    proxy_set_header Host $host;
    proxy_connect_timeout 5s;
    proxy_read_timeout 10s;
    add_header Cache-Control "no-store" always;
}
''')
    asset = (source / 'static/pipeline-drain-diagnostics.js').read_bytes()
    hook = '<script src="/pipeline-drain-diagnostics.js?v=' + hashlib.sha256(asset).hexdigest()[:12] + '"></script>'
    for root in (Path('/usr/share/nginx/html'), Path('/root/drama_material_service/static')):
        install(root / 'pipeline-drain-diagnostics.js', asset)
        html = root / 'pipeline-monitor.html'
        text = html.read_text()
        assert 'pipeline-drain-diagnostics.js' not in text
        assert '</body>' in text
        install(html, text.replace('</body>', hook + '\n</body>').encode())
    for unit in ('x-tag-selection-watchdog.service', 'x-tag-selection-watchdog.timer'):
        install(Path('/etc/systemd/system') / unit, (source / 'deploy' / unit).read_bytes())
    install('/etc/systemd/system/x-tag-auto.service.d/70-selection-recovery.conf', b'''[Service]
Environment=X_AUTO_EXECUTION_LOCK_DIR=/mnt/data-disk/x-tag-publisher/auto/execution-locks
''')
    user = pwd.getpwnam('x-post-daily')
    locks = Path('/mnt/data-disk/x-tag-publisher/auto/execution-locks')
    locks.mkdir(parents=True, exist_ok=True)
    os.chown(locks, user.pw_uid, user.pw_gid)
    locks.chmod(0o2770)
    link = Path('/opt/x-selection-recovery/current')
    link.parent.mkdir(parents=True, exist_ok=True)
    assert not link.exists()
    link.symlink_to(source)
    manifest['new_symlink'] = str(link)
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({'installed': True, 'manifest': str(backup / 'manifest.json')}))


if __name__ == '__main__':
    main()
