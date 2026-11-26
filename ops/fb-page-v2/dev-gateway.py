"""Install only the developer gateway; preserve the live Hong Kong selector."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

ROOT = Path('/mnt/data-disk/fb-page-v2/dev-gateway')
SSH = ROOT / 'ssh'
UNIT = 'fb-page-v2-dev-gateway.service'
HOST = '43.154.250.89'
HOST_FINGERPRINT = 'SHA256:mJwDEB2W3b50+ehFNyK4kUIWnjcR/XbFioRWEppJPEA'
MARKER = 'fb-page-v2-dev-gateway'
FILES = {
    'dev-gateway.service': Path('/etc/systemd/system') / UNIT,
    'dev-gateway.http.conf': Path('/etc/nginx/conf.d/fb-page-v2-dev-http.conf'),
    'dev-gateway.locations.conf': Path('/etc/nginx/default.d/fb-page-v2-dev.conf'),
    'dev-gateway.proxy.conf': Path('/etc/nginx/snippets/fb-page-v2-dev-proxy.conf'),
    'auth.conf': Path('/etc/nginx/snippets/fb-page-v2-dev-auth.conf'),
}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def atomic(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + str(os.getpid()))
    with tmp.open('xb') as f:
        os.fchmod(f.fileno(), mode)
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def public_line(public):
    parts = public.strip().split()
    if len(parts) != 3 or parts[0] != 'ssh-ed25519' or parts[2] != MARKER or not re.fullmatch(r'[A-Za-z0-9+/=]+', parts[1]):
        raise ValueError('invalid_gateway_public_key')
    return ('restrict,port-forwarding,permitopen="10.89.0.125:8944",'
            'permitlisten="127.0.0.1:1",command="/bin/false" ' + ' '.join(parts))


def prepare():
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk').stdout.decode().strip() == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    SSH.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = SSH / 'id_ed25519'
    if not key.exists():
        run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', MARKER, '-f', str(key))
    scanned = run('ssh-keyscan', '-T', '5', '-t', 'ed25519', HOST).stdout
    fingerprint = run('ssh-keygen', '-lf', '-', input=scanned).stdout.decode()
    assert HOST_FINGERPRINT in fingerprint and len(scanned.splitlines()) == 1, 'host_key_mismatch'
    atomic(SSH / 'known_hosts', scanned)
    public = key.with_suffix('.pub').read_text().strip()
    public_line(public)
    print(json.dumps({'public_key': public, 'host_key_verified': True}))


def install_key(public_file, backup, remove=False):
    # This action runs only on HK and grants a single forwarding destination.
    public = Path(public_file).read_text().strip()
    line = public_line(public).encode()
    target = Path('/root/.ssh/authorized_keys')
    before = target.read_bytes()
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    if remove:
        assert line in before.splitlines(), 'gateway_key_not_found'
        after = b'\n'.join(x for x in before.splitlines() if x != line) + b'\n'
    else:
        if line in before.splitlines():
            print('{"key_present":true}'); return
        assert not any(public.split()[1].encode() in x for x in before.splitlines()), 'key_already_has_different_permissions'
        after = before.rstrip(b'\n') + b'\n' + line + b'\n'
    name = 'authorized_keys.before_remove' if remove else 'authorized_keys.before'
    assert not (backup / name).exists(), 'backup_exists'
    atomic(backup / name, before)
    atomic(target, after)
    print(json.dumps({'key_removed' if remove else 'key_added': True, 'other_lines_preserved': True}))


def auth_snippet(env_file):
    token = None
    for line in Path(env_file).read_text().splitlines():
        if line.startswith('FB_PAGE_ASSIGNMENT_API_TOKEN='):
            token = line.split('=', 1)[1].strip().strip('"').strip("'")
    if not token or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{32,512}', token):
        raise ValueError('invalid_existing_bearer_configuration')
    # Exact string comparison avoids global Nginx map bucket-size dependencies.
    return ('if ($http_authorization != "Bearer ' + token + '") { return 401 \'{"error":"unauthorized"}\'; }\n').encode()


def apply(backup, env_file):
    assert (SSH / 'id_ed25519').is_file() and (SSH / 'known_hosts').is_file()
    assert not (backup / 'manifest.json').exists(), 'backup_exists'
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = Path(__file__).resolve().parent
    payloads = {name: auth_snippet(env_file) if name == 'auth.conf' else (source / name).read_bytes() for name in FILES}
    facts = {'source': str(source), 'files': {}, 'unit_active': subprocess.call(['systemctl', 'is-active', '--quiet', UNIT]) == 0,
             'unit_enabled': subprocess.call(['systemctl', 'is-enabled', '--quiet', UNIT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0}
    for name, path in FILES.items():
        facts['files'][name] = {'path': str(path), 'existed': path.exists(), 'installed_sha256': hashlib.sha256(payloads[name]).hexdigest()}
        if path.exists():
            atomic(backup / name, path.read_bytes())
    atomic(backup / 'manifest.json', json.dumps(facts, indent=2).encode())
    for name, path in FILES.items():
        atomic(path, payloads[name], 0o600 if name == 'auth.conf' else 0o644)
    try:
        run('systemd-analyze', 'verify', str(FILES['dev-gateway.service']))
        run('nginx', '-t')
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', UNIT)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(10):
            try:
                with opener.open('http://127.0.0.1:19845/healthz', timeout=3) as r:
                    assert json.load(r) == {'ok': True, 'version': 2}
                break
            except (OSError, ValueError):
                if attempt == 9: raise
                time.sleep(1)
        run('systemctl', 'reload', 'nginx')
    except Exception:
        rollback(backup)
        raise
    atomic(backup / 'activated.json', json.dumps({'tunnel_health': True, 'nginx_reloaded': True, 'at': time.time()}).encode())
    print('{"tunnel_health":true,"nginx_reloaded":true,"selector_restart":false}')


def rollback(backup):
    facts = json.loads((backup / 'manifest.json').read_text())
    for name, row in facts['files'].items():
        path = Path(row['path'])
        assert path == FILES[name]
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == row['installed_sha256'], 'changed_since_deployment'
    for name, row in facts['files'].items():
        if name == 'dev-gateway.service': continue
        path = FILES[name]
        if row['existed']: atomic(path, (backup / name).read_bytes(), 0o600 if name == 'auth.conf' else 0o644)
        else: path.unlink()
    run('nginx', '-t'); run('systemctl', 'reload', 'nginx')
    if not facts['unit_active']: run('systemctl', 'stop', UNIT)
    if not facts['unit_enabled']: run('systemctl', 'disable', UNIT)
    row = facts['files']['dev-gateway.service']
    if row['existed']: atomic(FILES['dev-gateway.service'], (backup / 'dev-gateway.service').read_bytes(), 0o644)
    else: FILES['dev-gateway.service'].unlink()
    run('systemctl', 'daemon-reload')
    print('{"gateway_rolled_back":true,"selector_changed":false}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'install-key', 'remove-key', 'apply', 'rollback'])
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--public-key-file', type=Path)
    parser.add_argument('--env', default='/etc/fb-page-assignment.env')
    args = parser.parse_args()
    if args.action == 'prepare': prepare()
    elif args.action in ('install-key', 'remove-key'):
        assert args.public_key_file and args.backup
        install_key(args.public_key_file, args.backup, remove=args.action == 'remove-key')
    elif args.action == 'apply':
        assert args.backup; apply(args.backup, args.env)
    else:
        assert args.backup; rollback(args.backup)
