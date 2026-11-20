"""Install/roll back the title API and UI; retain publication workers and all data."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('regular_deploy', REPO/'ops/youtube-regular/deploy.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
BASE = '1ae89954e254909191cf5fa1dc8eba8970611c62'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'rollback'))
    args = parser.parse_args()
    if d.cmd('findmnt', '-no', 'UUID', '--target', d.CPU_DATA).decode().strip() != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Expected mounted data disk')
    version = d.git(REPO, 'rev-parse', 'HEAD').decode().strip()
    folder = d.CPU_DATA/'youtube-channel-titles-20261120'/version
    installing = args.action == 'install'
    d.BASE['publisher'] = BASE
    options = SimpleNamespace(repo=REPO, scope='publisher', version=version, from_version=BASE)
    if installing:
        if (folder/'manifest.json').exists():
            record = json.loads((folder/'manifest.json').read_text())
            if record['state'] == 'active':
                for item in record['files']:
                    if d.digest(d.filebytes(Path(item['target']))) != item['new']:
                        raise RuntimeError('Later file change: '+item['target'])
                print('Already installed: '+version)
                return
            raise RuntimeError('Inspect the recorded incomplete deployment before resuming')
        folder.mkdir(parents=True, mode=0o700)
        os.chmod(folder, 0o700)
        record, plan = d.build(options, folder)
    else:
        record = json.loads((folder/'manifest.json').read_text())
        plan = {}
        for item in record['files']:
            target = Path(item['target'])
            before = d.filebytes(target)
            after = d.filebytes(folder/item['backup'])
            if d.digest(after) != item['old']:
                raise RuntimeError('Backup changed: '+str(target))
            if d.digest(before) == item['old']:
                continue
            if d.digest(before) != item['new']:
                raise RuntimeError('Review newer code before rollback: '+str(target))
            plan[target] = (before, after)
        if not plan:
            print('Already rolled back: '+version)
            return
    record['processes_before'] = {u: d.service(u) for u in (
        d.API_UNIT, d.PREPARATION, 'pipeline-youtube-canonical.service', 'drama-material-job-worker.service')}
    baseline = record['processes_before'][d.API_UNIT]
    if baseline['ActiveState'] != 'active' or baseline['MainPID'] == '0':
        raise RuntimeError('Main API is already transitioning')
    mark = args.action
    for lane in ('x', 'youtube'):
        d.cmd('python3', d.HEALTH, 'snapshot', '--lane', lane,
              '--output', folder/f'{lane}-before-{mark}.json')
    record['state'] = 'installing' if installing else 'rolling_back'
    d.save(folder, record)
    # Activate Python before serving the new UI contract. Use the installed service drain.
    for target, (before, after) in plan.items():
        if target.suffix != '.py':
            continue
        if d.filebytes(target) != before:
            raise RuntimeError('Concurrent code change: '+str(target))
        compile(after, str(target), 'exec')
        d.atomic(target, after)
    d.cmd('systemctl', 'restart', '--no-block', d.API_UNIT)
    d.await_restarts({d.API_UNIT: baseline['MainPID']})
    for lane in ('x', 'youtube'):
        d.cmd('python3', d.HEALTH, 'restore', '--baseline', folder/f'{lane}-before-{mark}.json',
              '--wait-seconds', '25', '--output', folder/f'{lane}-after-{mark}.json')
    job = 'drama-material-job-worker.service'
    if record['processes_before'][job]['ActiveState'] == 'active' and d.service(job)['ActiveState'] != 'active':
        d.cmd('systemctl', 'start', job)
    for target, (before, after) in plan.items():
        if target.suffix == '.py':
            continue
        if d.filebytes(target) != before:
            raise RuntimeError('Concurrent UI change: '+str(target))
        d.atomic(target, after)
    record.update(state='active' if installing else 'rolled_back',
                  processes_after={u: d.service(u) for u in record['processes_before']})
    d.save(folder, record)
    print(json.dumps({'state': record['state'], 'version': version, 'backup': str(folder)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
