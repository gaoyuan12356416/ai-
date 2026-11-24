#!/usr/bin/env python3
"""One-shot IKEv2 client in a temporary network namespace; JSON secret on stdin."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def run(args, check=True, timeout=25):
    return subprocess.run(args, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=timeout)


def check(config, root):
    namespace, host_link, client_link = 'iphone-vpn-check', 'ivpnhost', 'ivpnclient'
    if namespace in run(['ip', 'netns', 'list']).stdout or run(['ip', 'link', 'show', host_link], False).returncode == 0:
        raise RuntimeError('A previous VPN check is still present')
    temp = Path(tempfile.mkdtemp(prefix='tunnel-check-', dir=root))
    log_path = temp / 'client.log'
    dns_dir = Path('/etc/netns') / namespace
    if dns_dir.exists():
        raise RuntimeError('Namespace resolver directory is already owned')
    rules = [
        ['-t', 'filter', '-I', 'FORWARD', '1', '-i', host_link, '-s', '169.254.245.2/32', '-m', 'comment', '--comment', 'iphone-vpn-check', '-j', 'ACCEPT'],
        ['-t', 'filter', '-I', 'FORWARD', '1', '-o', host_link, '-d', '169.254.245.2/32', '-m', 'conntrack', '--ctstate', 'RELATED,ESTABLISHED', '-m', 'comment', '--comment', 'iphone-vpn-check', '-j', 'ACCEPT'],
        ['-t', 'nat', '-I', 'POSTROUTING', '1', '-s', '169.254.245.2/32', '-o', 'eth0', '-m', 'comment', '--comment', 'iphone-vpn-check', '-j', 'MASQUERADE'],
    ]
    installed = []
    process = None
    created = False
    link_created = False
    try:
        run(['ip', 'netns', 'add', namespace]); created = True
        run(['ip', 'link', 'add', host_link, 'type', 'veth', 'peer', 'name', client_link]); link_created = True
        run(['ip', 'link', 'set', client_link, 'netns', namespace])
        run(['ip', 'addr', 'add', '169.254.245.1/30', 'dev', host_link])
        run(['ip', 'link', 'set', host_link, 'up'])
        run(['ip', '-n', namespace, 'addr', 'add', '169.254.245.2/30', 'dev', client_link])
        run(['ip', '-n', namespace, 'link', 'set', client_link, 'up'])
        run(['ip', '-n', namespace, 'link', 'set', 'lo', 'up'])
        run(['ip', '-n', namespace, 'route', 'add', 'default', 'via', '169.254.245.1'])
        dns_dir.mkdir(parents=True)
        (dns_dir / 'resolv.conf').write_text('nameserver 1.1.1.1\nnameserver 8.8.8.8\n')
        for rule in rules:
            run(['iptables', '-w', '10'] + rule)
            installed.append(rule)
        conf = temp / 'strongswan.conf'
        conf.write_text('charon-cmd {\n load = random nonce aes sha1 sha2 hmac kdf openssl gmp kernel-netlink socket-default\n}\n')
        env = dict(os.environ, STRONGSWAN_CONF=str(conf))
        args = ['ip', 'netns', 'exec', namespace, 'charon-cmd', '--profile', 'ikev2-psk',
                '--host', config['public_ip'], '--identity', config['client_id'],
                '--remote-identity', config['public_ip'], '--ike-proposal', 'aes256-sha256-modp2048',
                '--esp-proposal', 'aes256-sha256', '--remote-ts', '0.0.0.0/0']
        with log_path.open('w') as log:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=log, stderr=log, env=env, start_new_session=True)
            process.stdin.write((config['psk'] + '\n').encode())
            process.stdin.close()
            ready = False
            for _ in range(90):
                addresses = json.loads(run(['ip', '-n', namespace, '-4', '-json', 'addr', 'show']).stdout)
                leased = any(ipaddress.ip_address(a['local']) in ipaddress.ip_network(config['pool'])
                             for link in addresses for a in link.get('addr_info', []))
                policy = run(['ip', '-n', namespace, 'xfrm', 'policy']).stdout
                if leased and 'dir out' in policy and 'proto esp' in policy:
                    ready = True
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.5)
            if not ready:
                raise RuntimeError('IKEv2 client did not establish; log: ' + str(log_path))
            egress = run(['ip', 'netns', 'exec', namespace, 'curl', '-4', '--noproxy', '*', '--max-time', '20', '-fsS', 'https://api.ipify.org']).stdout.strip()
            if egress != config['public_ip']:
                raise RuntimeError('Unexpected egress IP: ' + egress)
            status = run(['ip', 'netns', 'exec', namespace, 'curl', '-4', '--noproxy', '*', '--max-time', '20', '-sS', '-o', '/dev/null', '-w', '%{http_code}', 'https://www.google.com/generate_204']).stdout.strip()
            if status != '204':
                raise RuntimeError('Internet HTTP check failed: ' + status)
            result = {'server': config['public_ip'], 'ike_authentication': True, 'child_sa': True,
                      'egress_ip': egress, 'internet_http_status': status, 'log': str(log_path)}
            (temp / 'result.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(result))
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        for rule in reversed(installed):
            delete = list(rule)
            delete[2] = '-D'
            del delete[4]
            run(['iptables', '-w', '10'] + delete, False)
        if link_created:
            run(['ip', 'link', 'delete', host_link], False)
        if created:
            run(['ip', 'netns', 'delete', namespace], False)
        if dns_dir.exists():
            shutil.rmtree(str(dns_dir))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--root', required=True)
    a = p.parse_args()
    check(json.load(sys.stdin), a.root)
