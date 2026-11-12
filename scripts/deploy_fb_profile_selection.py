"""Install a GitHub-verified FB selection overlay, preserving frozen work."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release
from scripts.deploy_pipeline_runtime import verify_source

BASE = 'a872688895b6ff92ca9a5ad439a747d1daa653fb'
FILES = ['features/fb_auto_posts/' + name + '.py' for name in
         ('core', 'repositories', 'languages', 'profile_selection', 'profile_material')]
FILES += ['features/x_tag_revenue/' + name for name in
          ('__init__.py', 'core.py', 'tags.py', 'taxonomy.py', 'hierarchy.py', 'tag_dictionary.json')]
TESTS = ['tests.test_fb_profile_integration', 'tests.test_fb_profile_selection',
         'scripts.test_fb_hit_drama', 'scripts.test_fb_target_midnight', 'scripts.test_fb_auto_validation']


def verify(candidate, folder):
    """Import the installed modules and read current portraits as the service user."""
    code = '''import importlib,json,pathlib,sqlite3
from contextlib import closing
for name in ('core','repositories','languages','profile_selection','profile_material'):
 importlib.import_module('features.fb_auto_posts.'+name)
from features.fb_auto_posts.profile_selection import load_profiles,Decision,DEFAULT_ROOT
root=pathlib.Path(DEFAULT_ROOT)
pointer=json.loads((root/'current.json').read_text())
with closing(sqlite3.connect((root/pointer['database']).as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
 ids=[row[0] for row in db.execute('SELECT page_id FROM profiles ORDER BY page_id LIMIT 20')]
snapshot=load_profiles(ids)
assert ids and not snapshot[1].get('unavailable'),'portrait_read_failed'
decisions=[Decision(pid,['deployment-read-only'],snapshot) for pid in ids]
assert all(d.reason not in ('portrait_mapping_outdated','portrait_hierarchy_outdated') for d in decisions),'portrait_mapping_mismatch'
print(json.dumps(dict(profile_version=snapshot[1]['version'],pages_read=len(snapshot[0]['pages']),
 valid_profiles=sum(not d.reason for d in decisions),fallback_reasons=sorted(set(d.reason for d in decisions if d.reason)))))'''
    unit = 'fb-auto-post-service.service'
    user = release.run('systemctl', 'show', unit, '-p', 'User', '--value') or 'root'
    proof = subprocess.run(['runuser', '-u', user, '--', sys.executable, '-B', '-c', code],
                           cwd=candidate, capture_output=True, text=True, timeout=30)
    if proof.returncode:
        raise RuntimeError('service_user_profile_check_failed:' + proof.stderr[-1500:])
    result = json.loads(proof.stdout)
    release.save(folder / 'profile-read-check.json', result)
    return result


def prepare(folder, tests):
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 3 * 1024**3, 'insufficient_data_disk_space'
    commit = verify_source(SOURCE, 'HEAD')
    origin = release.run('git', '-C', SOURCE, 'remote', 'get-url', 'origin').rstrip('/')
    assert origin in ('https://github.com/gaoyuan12356416/ai-', 'https://github.com/gaoyuan12356416/ai-.git',
                      'git@github.com:gaoyuan12356416/ai-.git', 'ssh://git@github.com/gaoyuan12356416/ai-.git'), 'unexpected_source_repository'
    current, _, ledger = release.settings('fb')
    previous, candidate = current.resolve(), folder / 'candidate'
    assert current.is_symlink(), 'runtime_pointer_required'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    for target in [candidate, *candidate.rglob('*')]:
        original = previous / target.relative_to(candidate)
        if original.exists() and not target.is_symlink():
            stat = original.stat()
            os.chown(target, stat.st_uid, stat.st_gid)
    info = dict(platform='fb', commit=commit, baseline=BASE, source=str(SOURCE),
                previous=str(previous), candidate=str(candidate), files={}, static={}, status='prepared')
    for name in FILES:
        live, target = previous / name, candidate / name
        assert target.resolve().is_relative_to(candidate.resolve()), 'overlay_symlink_escape:' + name
        baseline = subprocess.run(['git', '-C', str(SOURCE), 'show', BASE + ':' + name], capture_output=True)
        if baseline.returncode == 0:
            assert live.read_bytes().replace(b'\r\n', b'\n') == baseline.stdout, 'baseline_changed:' + name
        else:
            assert not live.exists(), 'new_file_collision:' + name
        content = (SOURCE / name).read_bytes()
        if name.endswith('.py'):
            compile(content, name, 'exec')
        else:
            json.loads(content)
        release.atomic(target, content)
        info['files'][name] = dict(before=release.sha(live), after=release.sha(target))
    for module in tests:
        name = module.replace('.', '/') + '.py'
        assert (SOURCE / name).is_file(), 'test_module_missing:' + module
        release.atomic(candidate / name, (SOURCE / name).read_bytes())
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', *tests, '-q'],
                           cwd=candidate, capture_output=True, text=True, timeout=180)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr)
    assert proof.returncode == 0, 'candidate_tests_failed'
    info['profile_read_check'] = verify(candidate, folder)
    with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as src, \
            closing(sqlite3.connect(folder / 'ledger-before.sqlite3')) as dst:
        src.backup(dst)
    (folder / 'ledger-before.sqlite3').chmod(0o600)
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, folder=str(folder), files=len(FILES),
                          profile_read_check=info['profile_read_check'])), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback', 'verify'))
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--test-module', action='append')
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk'), 'data_disk_folder_required'
    if args.action == 'prepare':
        prepare(folder, args.test_module or TESTS)
    elif args.action == 'verify':
        info = json.loads((folder / 'manifest.json').read_text())
        current = release.verify_current(info, rollback=True)
        print(json.dumps(verify(current.resolve(), folder)), flush=True)
    else:
        release.activate('fb', folder, args.action == 'rollback')
        if args.action == 'apply':
            info = json.loads((folder / 'manifest.json').read_text())
            verify(Path(info['candidate']), folder)


if __name__ == '__main__':
    main()
