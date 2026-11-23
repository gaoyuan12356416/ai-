"""Use the established Mobo request/worker drain, then install a read-only warmer."""
import argparse
import json
import shlex
from pathlib import Path
import sys
import time

import deploy_moboreels_clicks as release

UNITS = ('moboreels-report-prewarm.service', 'moboreels-report-prewarm.timer')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'apply', 'rollback'))
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if not folder.is_relative_to('/mnt/data-disk') or release.run('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Required data mount unavailable')
    if args.action == 'stage':
        for unit in UNITS:
            if (Path('/etc/systemd/system') / unit).exists():
                raise RuntimeError('Existing warmer unit requires review')
        release.stage(folder, 'report-prewarm', '7c452a5db0795fec531d79e53086c6cab8632d60',
                      {'/root/drama_material_service/features/moboreels_library/effects.py':
                       '1831636938678524733dda6ee52b37f3c8a8343684c6a32fd7450e08392daa3e'})
        (folder / 'prewarm-units.json').write_text(json.dumps({u: release.digest(release.SOURCE / 'deploy' / u) for u in UNITS}))
        (folder / 'rollback.sh').write_text('#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(Path(__file__).resolve())) +
                                            ' rollback ' + shlex.quote(str(folder)) + '\n')
        return
    hashes = json.loads((folder / 'prewarm-units.json').read_text())
    if args.action == 'rollback':
        for unit in UNITS:
            if release.digest(Path('/etc/systemd/system') / unit) != hashes[unit]:
                raise RuntimeError('Warmer unit changed since deployment')
        release.run('systemctl', 'disable', '--now', UNITS[1])
        while release.run('systemctl', 'show', UNITS[0], '-p', 'ActiveState', '--value') in ('active', 'activating'):
            print('Waiting for the current read-only report warmup', flush=True)
            time.sleep(5)
        release.switch(folder, True)
        for unit in UNITS:
            (Path('/etc/systemd/system') / unit).unlink()
        release.run('systemctl', 'daemon-reload')
    else:
        release.switch(folder, False)
        for unit in UNITS:
            path = Path('/etc/systemd/system') / unit
            if path.exists():
                raise RuntimeError('Concurrent warmer unit installation')
            release.install(path, release.SOURCE / 'deploy' / unit)
        release.run('systemctl', 'daemon-reload')
        release.run('systemctl', 'enable', '--now', UNITS[1])
    print(json.dumps({'status': args.action, 'warmup_only': True, 'publishing_services_restarted': False}))


if __name__ == '__main__':
    main()
