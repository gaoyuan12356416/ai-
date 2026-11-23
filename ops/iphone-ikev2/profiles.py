#!/usr/bin/env python3
"""Generate removable iOS profiles; private keys never belong in this repository."""
import argparse
import json
from pathlib import Path
import plistlib
import uuid


def payload_id(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://private-vpn.gaoyuan/' + name)).upper()


def profile(configs, label):
    items = []
    for c in configs:
        identifier = 'com.gaoyuan.private-vpn.' + c['slug']
        items.append({
            'PayloadType': 'com.apple.vpn.managed', 'PayloadVersion': 1,
            'PayloadIdentifier': identifier, 'PayloadUUID': payload_id(identifier),
            'PayloadDisplayName': c['name'], 'UserDefinedName': c['name'],
            'VPNType': 'IKEv2', 'IPv4': {'OverridePrimary': 1},
            'IKEv2': {
                'RemoteAddress': c['public_ip'], 'RemoteIdentifier': c['public_ip'],
                'LocalIdentifier': c['client_id'], 'AuthenticationMethod': 'SharedSecret',
                'SharedSecret': c['psk'], 'ExtendedAuthEnabled': 0,
                'OnDemandEnabled': 0, 'EnablePFS': 0, 'DeadPeerDetectionRate': 'Medium',
                'IncludeAllNetworks': 1, 'ExcludeLocalNetworks': 0,
                'IKESecurityAssociationParameters': {'EncryptionAlgorithm': 'AES-256', 'IntegrityAlgorithm': 'SHA2-256', 'DiffieHellmanGroup': 14, 'LifeTimeInMinutes': 1440},
                'ChildSecurityAssociationParameters': {'EncryptionAlgorithm': 'AES-256', 'IntegrityAlgorithm': 'SHA2-256', 'DiffieHellmanGroup': 14, 'LifeTimeInMinutes': 480},
            },
        })
    key = 'com.gaoyuan.private-vpn.profile.' + '-'.join(c['slug'] for c in configs)
    return {'PayloadType': 'Configuration', 'PayloadVersion': 1, 'PayloadIdentifier': key,
            'PayloadUUID': payload_id(key), 'PayloadDisplayName': label,
            'PayloadDescription': 'Personal IKEv2 VPN connections. Select one server in Settings > VPN. This profile can be removed at any time.',
            'PayloadRemovalDisallowed': False, 'PayloadContent': items}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--config', action='append', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--name', default='CPU + Hong Kong GPU VPN')
    a = p.parse_args()
    configs = [json.loads(Path(f).read_text()) for f in a.config]
    data = plistlib.dumps(profile(configs, a.name), sort_keys=False)
    Path(a.output).write_bytes(data)
    assert plistlib.loads(data)['PayloadContent'][0]['VPNType'] == 'IKEv2'
    print('Generated {} VPN connection(s): {}'.format(len(configs), a.output))
