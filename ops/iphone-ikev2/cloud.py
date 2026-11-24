#!/usr/bin/env python3
"""Add only IKEv2 cloud ingress, with receipts and scoped rollback."""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
import urllib.request


class Cloud:
    def __init__(self, path):
        self.credentials = {k.lower().replace('_', ''): v for k, v in json.loads(Path(path).read_text()).items()}

    def call(self, service, region, action, payload):
        c = self.credentials
        host = service + '.tencentcloudapi.com'
        stamp = int(time.time())
        day = datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d')
        content_type = 'application/json; charset=utf-8'
        body = json.dumps(payload, separators=(',', ':')).encode()
        canonical = 'POST\n/\n\ncontent-type:' + content_type + '\nhost:' + host + '\n\ncontent-type;host\n' + hashlib.sha256(body).hexdigest()
        scope = day + '/' + service + '/tc3_request'
        string = 'TC3-HMAC-SHA256\n' + str(stamp) + '\n' + scope + '\n' + hashlib.sha256(canonical.encode()).hexdigest()
        def sign(key, value):
            return hmac.new(key, value.encode(), hashlib.sha256).digest()
        key = sign(sign(sign(('TC3' + c['secretkey']).encode(), day), service), 'tc3_request')
        headers = {'Content-Type': content_type, 'Host': host, 'X-TC-Action': action,
                   'X-TC-Version': '2017-03-12', 'X-TC-Region': region, 'X-TC-Timestamp': str(stamp),
                   'Authorization': 'TC3-HMAC-SHA256 Credential=' + c['secretid'] + '/' + scope + ', SignedHeaders=content-type;host, Signature=' + hmac.new(key, string.encode(), hashlib.sha256).hexdigest()}
        if c.get('token'):
            headers['X-TC-Token'] = c['token']
        with urllib.request.urlopen(urllib.request.Request('https://' + host, data=body, headers=headers), timeout=30) as response:
            result = json.load(response)['Response']
        if 'Error' in result:
            raise RuntimeError(action + ': ' + result['Error']['Code'])
        return result


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def owned_rule(port):
    return {'Protocol': 'UDP', 'Port': str(port), 'CidrBlock': '0.0.0.0/0',
            'Action': 'ACCEPT', 'PolicyDescription': 'iphone-ikev2-20261124'}


def matches(rule, expected):
    return all(str(rule.get(k, '')).upper() == str(v).upper() for k, v in expected.items())


def policies(api, region, group):
    return api.call('vpc', region, 'DescribeSecurityGroupPolicies', {'SecurityGroupId': group})['SecurityGroupPolicySet']


def instance(api, region, identifier):
    return api.call('cvm', region, 'DescribeInstances', {'InstanceIds': [identifier]})['InstanceSet'][0]


def prepare(api, path):
    record = json.loads(path.read_text()) if path.exists() else {'receipts': []}
    targets = [('cpu', 'na-ashburn', 'ins-lyr5109j', '43.166.187.96'),
               ('hk', 'ap-hongkong', 'ins-52tbyu6m', '43.154.250.89')]
    for name, region, identifier, address in targets:
        current = instance(api, region, identifier)
        if address not in current['PublicIpAddresses']:
            raise RuntimeError('Instance public address mismatch')
        if name not in record:
            record[name] = {'region': region, 'instance': identifier, 'groups_before': current['SecurityGroupIds'],
                            'policies_before': {g: policies(api, region, g) for g in current['SecurityGroupIds']}}
            save(path, record)
        item = record[name]
        if name == 'cpu':
            group = 'sg-hp7keb93'
            if group not in current['SecurityGroupIds']:
                raise RuntimeError('CPU security group changed')
            stats = api.call('vpc', region, 'DescribeSecurityGroupAssociationStatistics', {'SecurityGroupIds': [group]})['SecurityGroupAssociationStatisticsSet'][0]
            if stats['TotalCount'] != 1 or stats.get('SG', 0):
                raise RuntimeError('CPU security group is shared; use a dedicated group')
            now = policies(api, region, group)
            rules = [owned_rule(p) for p in (500, 4500) if not any(matches(r, owned_rule(p)) for r in now['Ingress'])]
            if rules:
                response = api.call('vpc', region, 'CreateSecurityGroupPolicies', {'SecurityGroupId': group, 'SecurityGroupPolicySet': {'Version': now['Version'], 'Ingress': rules}})
                record['receipts'].append({'action': 'add_cpu_rules', 'response': response})
                save(path, record)
        else:
            group_name = 'iphone-ikev2-ins-52tbyu6m-20261124'
            if not item.get('vpn_group'):
                found = api.call('vpc', region, 'DescribeSecurityGroups', {'Filters': [{'Name': 'security-group-name', 'Values': [group_name]}]})['SecurityGroupSet']
                if len(found) > 1:
                    raise RuntimeError('Multiple matching VPN groups; reconcile receipts')
                if found:
                    if found[0]['SecurityGroupDesc'] != 'Dedicated iPhone IKEv2 ingress for ins-52tbyu6m':
                        raise RuntimeError('Existing VPN group has a different owner')
                    group = found[0]['SecurityGroupId']
                else:
                    response = api.call('vpc', region, 'CreateSecurityGroupWithPolicies', {'GroupName': group_name,
                                        'GroupDescription': 'Dedicated iPhone IKEv2 ingress for ins-52tbyu6m',
                                        'SecurityGroupPolicySet': {'Ingress': [owned_rule(500), owned_rule(4500)]}})
                    group = response['SecurityGroup']['SecurityGroupId']
                    record['receipts'].append({'action': 'create_hk_group', 'response': response})
                item['vpn_group'] = group
                save(path, record)
            group = item['vpn_group']
            now = policies(api, region, group)
            if len(now['Ingress']) != 2 or now['Egress'] or not all(any(matches(r, owned_rule(p)) for r in now['Ingress']) for p in (500, 4500)):
                raise RuntimeError('Dedicated VPN group has unexpected rules')
            if group not in current['SecurityGroupIds']:
                response = api.call('cvm', region, 'AssociateSecurityGroups', {'InstanceIds': [identifier], 'SecurityGroupIds': [group]})
                record['receipts'].append({'action': 'associate_hk_group', 'response': response})
                save(path, record)
        item['policies_after'] = policies(api, region, group)
        item['groups_after'] = instance(api, region, identifier)['SecurityGroupIds']
        if not set(item['groups_before']).issubset(item['groups_after']):
            raise RuntimeError('An existing security group was removed')
        save(path, record)
        print(json.dumps({'server': name, 'vpn_group': group, 'groups': item['groups_after'], 'ports': [500, 4500]}))


def rollback(api, path):
    record = json.loads(path.read_text())
    cpu = record.get('cpu')
    if cpu:
        now = policies(api, cpu['region'], 'sg-hp7keb93')
        rules = [owned_rule(p) for p in (500, 4500) if any(matches(r, owned_rule(p)) for r in now['Ingress'])]
        if rules:
            response = api.call('vpc', cpu['region'], 'DeleteSecurityGroupPolicies', {'SecurityGroupId': 'sg-hp7keb93', 'SecurityGroupPolicySet': {'Version': now['Version'], 'Ingress': rules}})
            record['receipts'].append({'action': 'remove_cpu_rules', 'response': response})
            save(path, record)
    hk = record.get('hk')
    if hk and hk.get('vpn_group'):
        current = instance(api, hk['region'], hk['instance'])
        if hk['vpn_group'] in current['SecurityGroupIds']:
            if not set(hk['groups_before']).issubset(current['SecurityGroupIds']):
                raise RuntimeError('Original HK security groups changed; reconcile before removal')
            response = api.call('cvm', hk['region'], 'DisassociateSecurityGroups', {'InstanceIds': [hk['instance']], 'SecurityGroupIds': [hk['vpn_group']]})
            record['receipts'].append({'action': 'detach_hk_group', 'response': response})
            save(path, record)
    print('VPN cloud ingress rolled back; receipts and detached group retained.')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['prepare', 'rollback'])
    p.add_argument('--credentials', required=True)
    p.add_argument('--record', required=True)
    args = p.parse_args()
    {'prepare': prepare, 'rollback': rollback}[args.action](Cloud(args.credentials), Path(args.record))
