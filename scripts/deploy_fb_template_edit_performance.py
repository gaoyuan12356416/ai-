"""Overlay only template editing/read paths and JSON compression; retain ledgers."""
import argparse
from contextlib import closing
import difflib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release, deploy_fb_skipped_recheck as fb
from scripts.deploy_fb_profile_priority import external, owner_active
from scripts.deploy_pipeline_runtime import preserve_tree_owners, verify_source

BASE = '21e10416'
FILES = ['features/fb_auto_posts/' + name + '.py' for name in ('core', 'repositories', 'service')]
STATIC = ['static/fb-auto-publish-template' + ext for ext in ('.js', '.html')]
NGINX = Path('/etc/nginx/default.d/fb-auto-publish.conf')


def patched(path, relative):
    old = subprocess.check_output(['git', '-C', str(SOURCE), 'show', BASE + ':' + relative]).decode()
    new = (SOURCE / relative).read_text()
    current = path.read_text()
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    # Include context for insertions, and leave unrelated live overlays intact.
    for group in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_grouped_opcodes(2):
        before = ''.join(a[group[0][1]:group[-1][2]])
        after = ''.join(b[group[0][3]:group[-1][4]])
        if not before or current.count(before) != 1:
            raise RuntimeError('patch_context_changed:' + str(path))
        current = current.replace(before, after, 1)
    return current.encode()


def prepare(folder):
    commit = verify_source(SOURCE, 'HEAD')
    current, _, db = release.settings('fb')
    previous, candidate = current.resolve(), folder / 'candidate'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    preserve_tree_owners(previous, candidate)
    info = dict(platform='fb', commit=commit, source=str(SOURCE), previous=str(previous),
                candidate=str(candidate), files={}, static={}, status='prepared')
    for name in FILES + STATIC:
        dest = candidate / name
        data = patched(previous / name, name)
        if name.endswith('.py'):
            compile(data, name, 'exec')
        release.atomic(dest, data)
        info['files'][name] = dict(before=release.sha(previous / name), after=release.sha(dest))
        if name in STATIC:
            for root in (release.MAIN, release.WEB):
                external(folder, info, root / dest.name, patched(root / dest.name, name))
    nginx_new = folder / 'nginx.new'
    nginx_new.write_bytes((SOURCE / 'deploy/nginx/fb-auto-publish.conf').read_bytes())
    if NGINX.exists():
        raise RuntimeError('preserve_existing_nginx_config:' + str(NGINX))
    info['nginx'] = dict(path=str(NGINX), before='', after=release.sha(nginx_new), next=str(nginx_new))
    for name in ('tests/test_fb_template_edit_performance.py', 'scripts/test_fb_target_midnight.py',
                 'scripts/test_fb_auto_validation.py'):
        release.atomic(candidate / name, (SOURCE / name).read_bytes())
    env = dict(os.environ, STORY_COPY_ROOT=str(folder / 'test-story'), FB_AUTO_PROFILE_ROOT=str(folder / 'test-profile'))
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', '-q',
                            'tests.test_fb_template_edit_performance', 'scripts.test_fb_target_midnight'],
                           cwd=candidate, env=env, capture_output=True, text=True, timeout=120)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr)
    proof.check_returncode()
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(folder / 'ledger-before.sqlite3')) as dst:
        src.backup(dst)
    (folder / 'ledger-before.sqlite3').chmod(0o600)
    info['main_api_pid'] = release.run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value')
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, folder=str(folder))), flush=True)


def activate(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    row = info['nginx']
    allowed = {row['before'], row['after']} if rollback else {row['before']}
    if release.sha(NGINX) not in allowed:
        raise RuntimeError('nginx_config_changed')
    for name, record in info['static'].items():
        if release.sha(name) != record['after' if rollback else 'before']:
            raise RuntimeError('external_file_changed:' + name)
    # The reviewed owner switch drains accepted handlers, preserving independent
    # durable rendering claims, and restores only the prior FB schedulers.
    fb.native_active = owner_active
    release.activate('fb', folder, rollback, defer_plan_read=False)
    if rollback:
        NGINX.unlink(missing_ok=True)
    else:
        release.atomic(NGINX, Path(row['next']).read_bytes())
    try:
        release.run('nginx', '-t')
    except Exception:
        if rollback:
            release.atomic(NGINX, Path(row['next']).read_bytes())
        else:
            NGINX.unlink()
        raise
    release.run('systemctl', 'reload', 'nginx')
    assert release.run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value') == info['main_api_pid']
    print(json.dumps(dict(nginx='reloaded', main_api_restarted=False, rollback=rollback)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    if args.action == 'prepare':
        prepare(folder)
    else:
        activate(folder, args.action == 'rollback')
