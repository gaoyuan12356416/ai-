#!/usr/bin/env python3
"""Manage only the forwarding rules belonging to the iPhone IKEv2 service."""
import argparse
import json
import subprocess


def rules(c):
    pool, iface = c['pool'], c['interface']
    tag = ['-m', 'comment', '--comment', 'iphone-ikev2']
    return [
        ('filter', 'INPUT', ['-p', 'udp', '-m', 'multiport', '--dports', '500,4500'] + tag + ['-j', 'ACCEPT']),
        ('filter', 'FORWARD', ['-s', pool, '-m', 'policy', '--dir', 'in', '--pol', 'ipsec'] + tag + ['-j', 'ACCEPT']),
        ('filter', 'FORWARD', ['-d', pool, '-m', 'policy', '--dir', 'out', '--pol', 'ipsec', '-m', 'conntrack', '--ctstate', 'RELATED,ESTABLISHED'] + tag + ['-j', 'ACCEPT']),
        ('nat', 'POSTROUTING', ['-s', pool, '-o', iface, '-m', 'policy', '--dir', 'out', '--pol', 'none'] + tag + ['-j', 'MASQUERADE']),
        ('mangle', 'FORWARD', ['-s', pool, '-p', 'tcp', '--tcp-flags', 'SYN,RST', 'SYN', '-m', 'policy', '--dir', 'in', '--pol', 'ipsec'] + tag + ['-j', 'TCPMSS', '--clamp-mss-to-pmtu']),
        ('mangle', 'FORWARD', ['-d', pool, '-p', 'tcp', '--tcp-flags', 'SYN,RST', 'SYN', '-m', 'policy', '--dir', 'out', '--pol', 'ipsec'] + tag + ['-j', 'TCPMSS', '--clamp-mss-to-pmtu']),
    ]


def apply(c, action):
    if action == 'up':
        subprocess.run(['sysctl', '-q', '-w', 'net.ipv4.ip_forward=1'], check=True)
    for table, chain, rule in rules(c):
        prefix = ['iptables', '-w', '10', '-t', table]
        exists = subprocess.run(prefix + ['-C', chain] + rule, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if action == 'up' and not exists:
            subprocess.run(prefix + ['-I', chain, '1'] + rule, check=True)
        elif action == 'down' and exists:
            subprocess.run(prefix + ['-D', chain] + rule, check=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['up', 'down'])
    p.add_argument('--config', required=True)
    a = p.parse_args()
    with open(a.config) as f:
        apply(json.load(f), a.action)
