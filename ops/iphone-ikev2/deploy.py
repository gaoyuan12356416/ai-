#!/usr/bin/env python3
"""Scoped CentOS 8 deployment. Secrets come from a root-only runtime JSON file."""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

FILES = [Path('/etc/strongswan/ipsec.conf'), Path('/etc/strongswan/ipsec.secrets'), Path('/etc/systemd/system/iphone-ikev2-network.service'), Path('/etc/sysctl.d/91-iphone-ikev2.conf')]
SERVICE = 'strongswan-starter.service'
RPM_SHA256 = 'e556b30b6186d3fef4f45e5324291425c8a69e1db44120bbd39fb57dc914f583'


def run(args, check=True):
    return subprocess.run(args, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def write(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.iphone-tmp')
    temp.write_text(value)
    os.chmod(str(temp), mode)
    os.replace(str(temp), str(path))


def config_text(c):
    return '''# Managed by ops/iphone-ikev2; generated from private runtime data.
config setup
    uniqueids=never
conn iphone-native
    keyexchange=ikev2
    type=tunnel
    auto=add
    left=%defaultroute
    leftid={public_ip}
    leftauth=psk
    leftsubnet=0.0.0.0/0
    right=%any
    rightid={client_id}
    rightauth=psk
    rightsourceip={pool}
    rightdns=1.1.1.1,8.8.8.8
    ike=aes256-sha256-modp2048!
    esp=aes256-sha256!
    ikelifetime=24h
    lifetime=8h
    reauth=no
    dpddelay=30s
    dpdaction=clear
    fragmentation=yes
    mobike=yes
    forceencaps=yes
'''.format(**c)


def install(a, c):
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.parts[1:2] == ('mnt',):
        found = run(['findmnt', '-n', '-o', 'UUID', '-T', str(root)]).stdout.strip()
        if found != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
            raise RuntimeError('CPU data disk mount mismatch')
    if run(['systemctl', 'is-active', SERVICE], False).returncode == 0:
        raise RuntimeError('Existing active IPsec service must be reconciled before replacing configuration')
    if run(['systemctl', 'is-active', 'strongswan.service'], False).returncode == 0:
        raise RuntimeError('Another strongSwan owner is active')
    if hashlib.sha256(Path(a.rpm).read_bytes()).hexdigest() != RPM_SHA256:
        raise RuntimeError('RPM checksum mismatch')
    snap = root / 'deployments' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    snap.mkdir(parents=True, mode=0o700)
    record = {'source_commit': run(['git', '-C', str(Path(__file__).resolve().parents[2]), 'rev-parse', 'HEAD']).stdout.strip(), 'public_ip': c['public_ip'], 'runtime_config': a.config, 'forwarding_before': run(['sysctl', '-n', 'net.ipv4.ip_forward']).stdout.strip(), 'service_enabled_before': run(['systemctl', 'is-enabled', SERVICE], False).stdout.strip(), 'files': []}
    for target in FILES:
        entry = {'path': str(target), 'existed': target.exists()}
        if target.exists():
            backup = snap / target.name
            shutil.copy2(str(target), str(backup))
            entry['backup'] = str(backup)
        record['files'].append(entry)
    write(snap / 'manifest.json', json.dumps(record, indent=2))
    write(snap / 'iptables-before.txt', run(['iptables-save']).stdout)
    result = run(['dnf', '-y', '--setopt=cachedir=' + str(root / 'pkgcache'), '--setopt=install_weak_deps=False', '--setopt=timeout=20', '--setopt=retries=1', 'install', a.rpm], False)
    write(snap / 'package-install.log', result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError('Package installation failed; see ' + str(snap / 'package-install.log'))
    write(FILES[0], config_text(c))
    write(FILES[1], '{} {} : PSK "{}"\n'.format(c['public_ip'], c['client_id'], c['psk']))
    script = Path(__file__).with_name('network.py').resolve()
    unit = '''[Unit]
Description=Forwarding for the iPhone IKEv2 VPN
After=network-online.target
Wants=network-online.target
Before=strongswan-starter.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 {script} up --config {config}
ExecStop=/usr/bin/python3 {script} down --config {config}
[Install]
WantedBy=multi-user.target
'''.format(script=script, config=a.config)
    write(FILES[2], unit, 0o644)
    write(FILES[3], '# iPhone IKEv2 forwarding\nnet.ipv4.ip_forward=1\n', 0o644)
    record['installed_hashes'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in FILES}
    write(snap / 'manifest.json', json.dumps(record, indent=2))
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'iphone-ikev2-network.service'])
    run(['systemctl', 'enable', '--now', SERVICE])
    run(['systemctl', 'is-active', SERVICE])
    write(root / 'latest-deployment.txt', str(snap) + '\n')
    print(json.dumps({'deployment': str(snap), 'source_commit': record['source_commit'], 'service': SERVICE, 'active': True}))


def rollback(a):
    snap = Path(a.deployment).resolve()
    record = json.loads((snap / 'manifest.json').read_text())
    for path, digest in record.get('installed_hashes', {}).items():
        p = Path(path)
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Configuration changed after deployment: ' + path)
    run(['systemctl', 'disable', '--now', SERVICE], False)
    run(['systemctl', 'disable', '--now', 'iphone-ikev2-network.service'], False)
    for item in record['files']:
        target = Path(item['path'])
        if item['existed']:
            shutil.copy2(item['backup'], str(target))
        elif target.exists():
            target.unlink()
    run(['systemctl', 'daemon-reload'])
    if record['forwarding_before'] == '0':
        run(['sysctl', '-q', '-w', 'net.ipv4.ip_forward=0'])
    if record['service_enabled_before'] == 'enabled':
        run(['systemctl', 'enable', SERVICE])
    print(json.dumps({'rolled_back': str(snap), 'package_retained': True, 'runtime_credentials_retained': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'rollback'])
    parser.add_argument('--config')
    parser.add_argument('--root')
    parser.add_argument('--rpm')
    parser.add_argument('--deployment')
    args = parser.parse_args()
    if args.action == 'rollback':
        rollback(args)
    else:
        c = json.loads(Path(args.config).read_text())
        ipaddress.ip_address(c['public_ip'])
        ipaddress.ip_network(c['pool'])
        assert re.fullmatch(r'[A-Za-z0-9@._-]+', c['client_id'])
        assert re.fullmatch(r'[A-Za-z0-9_-]{43,}', c['psk'])
        assert re.fullmatch(r'[A-Za-z0-9_.-]+', c['interface'])
        install(args, c)
