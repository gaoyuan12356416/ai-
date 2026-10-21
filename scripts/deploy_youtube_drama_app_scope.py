"""Install only product-scoped drama lookup; preserve current publishing state."""
import sys
from pathlib import Path
import deploy_youtube_channel_templates as common


RELATIVE_PATH = 'features/youtube_auto_publish/drama.py'
EXPECTED = '096e6111610322fdb77499d484db1496a1af44c5d4240e1e1e02000b6e9d4147'
WORKER = 'youtube-auto-publish-worker.service'
PROTECTED = ('drama-youtube-unified-writer.service', 'drama-youtube-publish-worker.service')


def prepare(stage):
    path = common.ROOT / RELATIVE_PATH
    if common.sha(path) != EXPECTED:
        raise RuntimeError('Live drama resolver changed; review before deployment')
    data = (stage / RELATIVE_PATH).read_bytes()
    compile(data, RELATIVE_PATH, 'exec')
    return {path: dict(data=data, old=EXPECTED, new=common.digest(data),
                       mode=path.stat().st_mode & 0o777)}


def main():
    import fcntl
    if common.run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data disk mount mismatch')
    with (common.BASE / 'drama-app-scope-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        protected = {unit: common.run('systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ActiveState')
                     for unit in PROTECTED}
        worker_pid = int(common.run('systemctl', 'show', WORKER, '-p', 'MainPID', '--value'))
        if worker_pid <= 0 or common.run('systemctl', 'show', WORKER, '-p', 'Restart', '--value') != 'always':
            raise RuntimeError('Worker must support graceful restart')
        common.prepare = prepare
        common.main()
        if '--check' not in sys.argv:
            # Finish in-flight generation/publication before Restart=always
            # starts the new process. Do not impose systemctl stop's kill timeout.
            common.run('systemctl', 'kill', '--kill-who=main', '--signal=SIGTERM', WORKER)
            print(common.json.dumps({'worker_draining_pid': worker_pid,
                                     'worker_restart': 'verify a new active PID before acceptance'}))
        for unit, state in protected.items():
            if common.run('systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ActiveState') != state:
                raise RuntimeError('Unrelated publisher process changed: ' + unit)


if __name__ == '__main__':
    main()
