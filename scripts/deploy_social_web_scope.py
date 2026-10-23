#!/usr/bin/env python3
"""Deploy only the social report reader and page; never touch publisher state."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess

SOURCE = Path(__file__).resolve().parents[1]
BASE = Path('/mnt/data-disk/social-analytics')
MAIN = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
UNIT = 'drama-material-api.service'
BASELINE = {
    'features/social_analytics/routes.py': '6631a8acf0f254d23a383ab7e1731263968a5145a8b3747ba9b1ec2416d0adf5',
    'features/social_analytics/facebook.py': '2474ca61d1d02b5f7be18b6364c5dfc42503929a3d0b75f4e18b7688ef893054',
    'features/social_analytics/report.py': 'f3c007eef669b555b197c3482a33832567ac43a24a0689ab74efdde705b283ad',
    'features/social_analytics/backend_scope.py': None,
    'static/social-analytics.html': 'bb51138c21e0b9ca748dfed114139174f198b05b59c94870e2d7041f68bb9c53',
}
UNCHANGED = {
    'features/social_analytics/adapters.py': 'b511ee11b12476fbe34912534c90c5f62b3bc07397360e0b9b7a97d4f35d3ac5',
    'static/social-analytics.js': 'bfe0a1bc79d172bc25f3ceb13a7dd8bd268d38ccd64782d6270567c945956fb3',
}
PUBLISHERS = ('fb-auto-post-service.service', 'fb-video-publish-worker.service',
              'x-post-automation.service', 'x-auto-post-service.service')
DAILY = Path('/opt/youtube-publisher-daily-report/current')


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('command_failed:' + args[0] + ':' + str(result.returncode))
    return result.stdout.strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def targets():
    result = {str(MAIN / relative): relative for relative in BASELINE}
    result[str(WEB / 'social-analytics.html')] = 'static/social-analytics.html'
    return result


def publisher_pids():
    return {u: command('systemctl', 'show', u, '-p', 'MainPID', '--value') for u in PUBLISHERS}


def save(data):
    path = Path(data['backup']) / 'deployment.json'
    temp = path.with_suffix('.next')
    temp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(path)


def load(backup):
    root = Path(backup).resolve()
    if root.parent != (BASE / 'backups').resolve():
        raise RuntimeError('invalid_backup')
    data = json.loads((root / 'deployment.json').read_text())
    if Path(data['backup']).resolve() != root or set(data['files']) != set(targets()):
        raise RuntimeError('manifest_target_mismatch')
    for target, info in data['files'].items():
        if info['relative'] != targets()[target]:
            raise RuntimeError('manifest_source_mismatch')
    return data


def copy_atomic(source, target, mode):
    target = Path(target)
    temporary = target.with_name(target.name + '.web-scope-next')
    shutil.copyfile(source, temporary)
    temporary.chmod(mode)
    temporary.replace(target)


def stage():
    if command('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_missing')
    if shutil.disk_usage(BASE).free < 1024 ** 3:
        raise RuntimeError('insufficient_disk')
    commit = command('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    if command('git', '-C', str(SOURCE), 'status', '--porcelain'):
        raise RuntimeError('source_dirty')
    if not command('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit):
        raise RuntimeError('source_not_fetched_from_github')
    for target, relative in targets().items():
        if sha(target) != BASELINE[relative]:
            raise RuntimeError('baseline_changed:' + target)
    for relative, expected in UNCHANGED.items():
        if sha(MAIN / relative) != expected:
            raise RuntimeError('dependency_changed:' + relative)
    command('/usr/bin/python3', '-B', '-c',
            "import sys;sys.path.insert(0," + repr(str(SOURCE)) + ");import features.social_analytics.routes")
    backup = BASE / 'backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-web-scope-' + commit[:12])
    backup.mkdir(mode=0o700)
    files = {}
    for i, (target, relative) in enumerate(targets().items()):
        path = Path(target)
        old = sha(path)
        saved = str(i) + '.before'
        mode = stat.S_IMODE(path.stat().st_mode) if old is not None else 0o644
        if old is not None:
            shutil.copy2(path, backup / saved)
        files[target] = dict(relative=relative, before=old, after=sha(SOURCE / relative), saved=saved, mode=mode)
    data = dict(backup=str(backup), source=str(SOURCE), commit=commit, files=files,
                status='prepared', publisher_pids=publisher_pids(), daily_release=str(DAILY.resolve()))
    save(data)
    return data


def restore(data):
    for target, info in data['files'].items():
        if sha(target) not in {info['before'], info['after']}:
            raise RuntimeError('rollback_target_drift:' + target)
        if info['before'] is not None and sha(Path(data['backup']) / info['saved']) != info['before']:
            raise RuntimeError('backup_corrupt')
    for target, info in data['files'].items():
        if info['before'] is None:
            Path(target).unlink(missing_ok=True)
        else:
            copy_atomic(Path(data['backup']) / info['saved'], target, info['mode'])
    command('systemctl', 'restart', UNIT)
    data['status'] = 'rolled_back'
    save(data)


def activate(data):
    if data['status'] != 'prepared':
        raise RuntimeError('not_prepared')
    for target, info in data['files'].items():
        if sha(target) != info['before'] or sha(Path(data['source']) / info['relative']) != info['after']:
            raise RuntimeError('activation_drift:' + target)
    data['publisher_pids'] = publisher_pids()
    data['daily_release'] = str(DAILY.resolve())
    data['status'] = 'activating'
    save(data)
    try:
        for target, info in data['files'].items():
            copy_atomic(Path(data['source']) / info['relative'], target, info['mode'])
        command('/usr/bin/python3', '-B', '-c',
                "import sys;sys.path.insert(0," + repr(str(MAIN)) + ");import features.social_analytics.routes")
        command('systemctl', 'restart', UNIT)
        if command('systemctl', 'show', UNIT, '-p', 'ActiveState', '--value') != 'active':
            raise RuntimeError('api_not_active')
        for target, info in data['files'].items():
            if sha(target) != info['after']:
                raise RuntimeError('readback_mismatch:' + target)
        if publisher_pids() != data['publisher_pids'] or str(DAILY.resolve()) != data['daily_release']:
            raise RuntimeError('independent_service_changed_during_deploy')
        data['status'] = 'active_pending_http_acceptance'
        data['api_pid'] = command('systemctl', 'show', UNIT, '-p', 'MainPID', '--value')
        save(data)
    except Exception:
        restore(data)
        raise
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['stage', 'activate', 'rollback'])
    parser.add_argument('--backup')
    args = parser.parse_args()
    if args.action == 'stage':
        data = stage()
    else:
        data = load(args.backup)
        if args.action == 'activate':
            activate(data)
        else:
            restore(data)
    print(json.dumps({key: data.get(key) for key in ['status', 'commit', 'backup', 'api_pid']}))


if __name__ == '__main__':
    main()
