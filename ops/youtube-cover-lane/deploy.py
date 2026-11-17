"""Install one preparation-only node; never restart existing production units."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import urllib.request

BASE = Path('/mnt/data-disk/youtube-cover-lane-20261117')
NODE = 'cpu-youtube-cover'
RUNTIME = Path('/mnt/data-disk/pipeline-dispatch-runtime/fb-lease-f3bd38531f6d/node')


def service_env(unit):
    pid = subprocess.check_output(['systemctl', 'show', unit, '-p', 'MainPID', '--value'], text=True).strip()
    return dict(x.split('=', 1) for x in Path('/proc', pid, 'environ').read_text().split('\0') if '=' in x)


def request(path, payload, token):
    req = urllib.request.Request('http://127.0.0.1:18846' + path,
        data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + token,
                                                   'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    if (BASE / 'deployment.json').exists():
        raise RuntimeError('already deployed; inspect evidence instead of replaying enrollment')
    source = Path(__file__).parent
    release = BASE / 'release'
    release.mkdir()
    runtime = release / 'runtime'
    shutil.copytree(RUNTIME, runtime, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc', 'backups'))
    # Use current approved provenance/image handling, without altering the shared node.
    shutil.copytree('/root/drama_material_service/features/youtube_auto_publish',
                    runtime / 'features/youtube_auto_publish', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree('/root/drama_material_service/features/moboreels_library',
                    runtime / 'features/moboreels_library', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(source / 'worker.py', release / 'worker.py')
    fingerprint = {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in runtime.rglob('*.py')}
    (BASE / 'runtime-manifest.json').write_text(json.dumps(fingerprint, indent=2))
    owner = pwd.getpwnam('pipeline-worker')
    for name in ('private', 'state'):
        path = BASE / name
        path.mkdir(mode=0o700)
        os.chown(path, owner.pw_uid, owner.pw_gid)
    with sqlite3.connect('file:/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3?mode=ro', uri=True) as c:
        before = c.execute('SELECT version,config_json FROM pipeline_config WHERE id=1').fetchone()
        if c.execute('SELECT 1 FROM pipeline_node_auth WHERE node_id=?', (NODE,)).fetchone():
            raise RuntimeError('node already enrolled; manual reconciliation required')
    (BASE / 'capacity-before.json').write_text(json.dumps({'version': before[0], 'config': json.loads(before[1])}, indent=2))
    with sqlite3.connect('file:/root/drama_material_service/data/drama_material_jobs.sqlite3?mode=ro', uri=True) as c:
        users = c.execute("SELECT user_id FROM drama_admin_user WHERE name='郜远' AND role='admin' AND status='active'").fetchall()
    if len(users) != 1:
        raise RuntimeError('requester identity not unique')
    actor = {'user_id': users[0][0], 'is_admin': True}
    internal = service_env('pipeline-coordinator.service')['PIPELINE_DISPATCH_INTERNAL_TOKEN']
    enrollment = request('/internal/pipeline-monitor/nodes/enroll', {'label': NODE, 'actor': actor}, internal)
    credential = request('/internal/pipeline/nodes/enroll', {'node_id': NODE,
                        'coordinator_url': 'http://127.0.0.1:18846'}, enrollment['token'])
    credential_path = BASE / 'private/credentials.json'
    fd = os.open(credential_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(credential, f)
    os.chown(credential_path, owner.pw_uid, owner.pw_gid)
    request('/internal/pipeline/nodes/register', {'node_id': NODE,
        'capabilities': ['backend:cpu', 'prepare:youtube', 'youtube_prepare'],
        'metrics': {'cpu_count': 1, 'backend': 'cpu'}}, credential['node_token'])
    for stage, count in [('prepare', 1), ('publish', 0)]:
        with sqlite3.connect('file:/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3?mode=ro', uri=True) as c:
            version = c.execute('SELECT version FROM pipeline_config WHERE id=1').fetchone()[0]
        request('/internal/pipeline-monitor/capacity', {'actor': actor, 'expected_version': version,
            'host_id': NODE, 'stage': stage, 'max_workers': count, 'scaling_mode': 'manual'}, internal)
    old_env = service_env('pipeline-node-cpu-main.service')
    env = {key: value for key, value in old_env.items() if key in
           ['PATH', 'HOME', 'CODEX_HOME', 'YOUTUBE_AUTO_CODEX_BIN', 'COS_BUCKET',
            'COS_REGION', 'COS_DOMAIN', 'COS_SECRET_ID', 'COS_SECRET_KEY']
           or key.startswith('X_POST_MEDIA_REPAIR_')}
    env.update(PIPELINE_NODE_ID=NODE, PIPELINE_NODE_STATE_ROOT=str(BASE / 'state'),
        PIPELINE_NODE_CONFIG=str(credential_path), PIPELINE_RENDER_BACKEND='cpu',
        PIPELINE_EXECUTORS='youtube_prepare', PIPELINE_RENDER_PROFILES='',
        PIPELINE_COORDINATOR_URL='http://127.0.0.1:18846',
        YOUTUBE_COVER_LANE_RUNTIME=str(runtime), PYTHONDONTWRITEBYTECODE='1',
        TMPDIR=str(BASE / 'state/tmp'))
    env['X_POST_MEDIA_REPAIR_WORK_ROOT'] = str(BASE / 'state/artifacts')
    (BASE / 'state/tmp').mkdir(mode=0o700)
    os.chown(BASE / 'state/tmp', owner.pw_uid, owner.pw_gid)
    env_path = BASE / 'private/worker.env'
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        for key, value in env.items():
            f.write(key + '=' + json.dumps(value) + '\n')
    unit = Path('/etc/systemd/system/youtube-cover-lane.service')
    if unit.exists():
        raise RuntimeError('unexpected existing unit')
    shutil.copy2(source / unit.name, unit)
    evidence = {'node_id': NODE, 'prepare': 1, 'publish': 0,
        'source_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
        'unit': str(unit), 'runtime_manifest': str(BASE / 'runtime-manifest.json')}
    (BASE / 'deployment.json').write_text(json.dumps(evidence, indent=2))
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', unit.name], check=True)
    print(json.dumps(evidence))


if __name__ == '__main__':
    main()
