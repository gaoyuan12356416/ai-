"""Small classification/Portuguese overlay plus the existing derived-profile refresh."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release
from scripts.deploy_hit_drama_resource_fix import prepare as prepare_overlay
from scripts.deploy_x_hierarchy import lock_file

BASE = 'f816ac469d3bcc694f110448998bf0a1fe693727'
MAIN = Path('/root/drama_material_service')
PORTRAITS = Path('/mnt/data-disk/x-channel-tag-revenue')
FILES = ['features/hit_drama_target.py', 'features/x_tag_revenue/taxonomy.py']
MAIN_FILES = ['features/x_tag_revenue/taxonomy.py', 'features/x_tag_revenue/refresh.py']


def prepare(folder):
    prepare_overlay('x', folder, base=BASE, files=FILES, static=False,
                    tests=['test_hit_drama_target', 'test_x_hit_drama', 'test_x_hierarchy'])
    info = json.loads((folder / 'manifest.json').read_text())
    info['main_files'] = {}
    for index, name in enumerate(MAIN_FILES):
        target = MAIN / name
        old = subprocess.run(['git', '-C', str(SOURCE), 'show', BASE + ':' + name],
                             capture_output=True, check=True).stdout.decode()
        new = (SOURCE / name).read_text()
        live = target.read_text()
        if name.endswith('taxonomy.py'):
            # Main's classification collector has a separately maintained query
            # implementation. Change only the reviewed shared classification rules.
            anchor = 'def collect_classifications('
            old, new = old.split(anchor, 1)[0], new.split(anchor, 1)[0]
            assert live.startswith(old), 'main_classification_drift'
            content = new + live[len(old):]
        else:
            assert live == old, 'main_baseline_drift:' + name
            content = new
        compile(content, name, 'exec')
        backup, next_file = folder / ('main-' + str(index) + '.old'), folder / ('main-' + str(index) + '.new')
        shutil.copy2(target, backup)
        next_file.write_text(content)
        info['main_files'][str(target)] = dict(before=release.sha(backup), after=release.sha(next_file), backup=str(backup), next=str(next_file))
    pointer = json.loads((PORTRAITS / 'current.json').read_text())
    shutil.copy2(PORTRAITS / pointer['file'], folder / 'portrait-before.json')
    release.save(folder / 'portrait-pointer-before.json', pointer)
    release.save(folder / 'manifest.json', info)


def main_files(info, rollback=False):
    with lock_file(PORTRAITS / 'refresh.lock'):
        for name, row in info['main_files'].items():
            allowed = [row['after']] if rollback else [row['before'], row['after']]
            assert release.sha(name) in allowed, 'main_code_changed:' + name
        for name, row in info['main_files'].items():
            release.atomic(name, Path(row['backup' if rollback else 'next']).read_bytes())


def apply(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    if rollback:
        release.activate('x', folder, True)
        main_files(info, True)
    else:
        # A refresh already using old rules finishes under its original lock.
        # Never stop the collector or publish a partial portrait.
        main_files(info)
        if info['status'] != 'active':
            release.activate('x', folder)
    release.run('systemctl', 'start', '--no-block', 'x-channel-tag-revenue-refresh.service')
    print(json.dumps(dict(status='rollback_refresh_started' if rollback else 'active_refresh_started',
                          folder=str(folder), commit=info['commit'])), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback'])
    parser.add_argument('--folder', required=True)
    args = parser.parse_args()
    folder = Path(args.folder).resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    if args.action == 'prepare':
        prepare(folder)
    else:
        apply(folder, args.action == 'rollback')
