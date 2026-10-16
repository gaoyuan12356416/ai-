"""Append only the verified business CDN; no media jobs or publishing calls."""
import json
from pathlib import Path
import subprocess
import sys

UNITS = {'tt-gpu-publisher.service': 'TT_POST_GPU_ALLOWED_SOURCE_HOSTS',
         'tt-gpu-direct-outro.service': 'TT_POST_GPU_ALLOWED_SOURCE_HOSTS',
         'fb-page-random-overlay-gpu.service': 'FB_PAGE_GPU_ALLOWED_SOURCE_HOSTS'}
CDN = 'socialkit-cdn.yingliang.tech'


def run(args):
    return subprocess.check_output(args).decode().strip()


def hosts(unit, key):
    pid = run(['systemctl', 'show', unit, '-p', 'MainPID', '--value'])
    assert pid != '0', unit
    env = dict(value.split('=', 1) for value in Path('/proc/' + pid + '/environ').read_bytes().decode().split('\0') if '=' in value)
    return [host for host in env[key].split(',') if host]


def files(unit):
    name = unit.removesuffix('.service')
    return (Path('/etc/post-repair-source-hosts-20261016-' + name + '.env'),
            Path('/etc/systemd/system/' + unit + '.d/99-zzzz-source-hosts-20261016.conf'))


if __name__ == '__main__':
    mode, stamp = sys.argv[1:]
    backup = Path('/data/post-repair-20261016/backups') / stamp
    if mode == 'apply':
        backup.mkdir(parents=True, exist_ok=False, mode=0o700)
        before = {unit: hosts(unit, key) for unit, key in UNITS.items()}
        (backup / 'previous-source-hosts.json').write_text(json.dumps(before, indent=2))
        for unit, key in UNITS.items():
            env, dropin = files(unit)
            assert not env.exists() and not dropin.exists(), unit
            values = list(dict.fromkeys(before[unit] + [CDN]))
            assert all(host and all(ch.isalnum() or ch in '.-' for ch in host) for host in values)
            env.write_text(key + '=' + ','.join(values) + '\n')
            env.chmod(0o600)
            dropin.parent.mkdir(parents=True, exist_ok=True)
            dropin.write_text('[Service]\nEnvironmentFile=' + str(env) + '\n')
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'restart', *UNITS], check=True, timeout=90)
        after = {unit: hosts(unit, key) for unit, key in UNITS.items()}
        assert all(CDN in value and set(before[unit]).issubset(value) for unit, value in after.items())
        (backup / 'verified-source-hosts.json').write_text(json.dumps(after, indent=2))
        print(json.dumps({'status': 'applied', 'backup': str(backup), 'safe_running_hosts': after}))
    elif mode == 'rollback':
        assert (backup / 'previous-source-hosts.json').exists()
        for unit in UNITS:
            env, dropin = files(unit)
            dropin.unlink(missing_ok=True)
            env.unlink(missing_ok=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'restart', *UNITS], check=True, timeout=90)
        print(json.dumps({'status': 'rolled_back', 'safe_running_hosts': {unit: hosts(unit, key) for unit, key in UNITS.items()}}))
