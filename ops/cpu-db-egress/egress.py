"""Scoped live bandwidth-cap repair for the CPU instance; no service restarts.

Reads secrets only on CPU, stores sanitized cloud receipts on the data disk,
and never repeats an uncertain resource write. Existing traffic billing stays.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


INSTANCE = 'ins-lyr5109j'
REGION = 'na-ashburn'
PUBLIC_IP = '43.166.187.96'
BEFORE_MBPS = 100
AFTER_MBPS = 200
BILLING = 'TRAFFIC_POSTPAID_BY_HOUR'
DATA = Path('/mnt/data-disk/cpu-db-network')
UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
SECRET = Path('/root/.config/tencentcloud-billing/credentials.json')


def need(condition, code):
    if not condition:
        raise RuntimeError(code)


def atomic_json(path, value):
    temporary = path.with_suffix('.next')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Cloud:
    def __init__(self, secret=SECRET):
        values = {key.lower().replace('_', ''): value
                  for key, value in json.loads(secret.read_text()).items()}
        self.secret_id, self.secret_key = values['secretid'], values['secretkey']
        self.token = values.get('token')

    def api(self, action, payload):
        need(action in {'DescribeInstances', 'InquiryPriceResetInstancesInternetMaxBandwidth',
                        'ResetInstancesInternetMaxBandwidth'}, 'unsupported_cloud_action')
        need(payload.get('InstanceIds') == [INSTANCE], 'unexpected_instance_target')
        if action != 'DescribeInstances':
            need(payload.get('InternetAccessible') in (
                {'InternetMaxBandwidthOut': BEFORE_MBPS}, {'InternetMaxBandwidthOut': AFTER_MBPS}),
                'unexpected_bandwidth_target')
            need(set(payload) == {'InstanceIds', 'InternetAccessible'}, 'unexpected_resource_fields')
        host, service = 'cvm.tencentcloudapi.com', 'cvm'
        stamp = int(time.time())
        day = datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d')
        content_type = 'application/json; charset=utf-8'
        body = json.dumps(payload, separators=(',', ':')).encode()
        canonical = ('POST\n/\n\ncontent-type:' + content_type + '\nhost:' + host +
                     '\n\ncontent-type;host\n' + hashlib.sha256(body).hexdigest())
        scope = day + '/' + service + '/tc3_request'
        to_sign = ('TC3-HMAC-SHA256\n' + str(stamp) + '\n' + scope + '\n' +
                   hashlib.sha256(canonical.encode()).hexdigest())
        def sign(key, value):
            return hmac.new(key, value.encode(), hashlib.sha256).digest()
        signing_key = sign(sign(sign(('TC3' + self.secret_key).encode(), day), service), 'tc3_request')
        signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers = {'Content-Type': content_type, 'Host': host, 'X-TC-Action': action,
                   'X-TC-Version': '2017-03-12', 'X-TC-Region': REGION,
                   'X-TC-Timestamp': str(stamp),
                   'Authorization': 'TC3-HMAC-SHA256 Credential=' + self.secret_id + '/' + scope +
                                    ', SignedHeaders=content-type;host, Signature=' + signature}
        if self.token:
            headers['X-TC-Token'] = self.token
        request = urllib.request.Request('https://' + host, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)['Response']
        except urllib.error.HTTPError as exc:
            raise RuntimeError('cloud_http_' + str(exc.code)) from None
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError('cloud_transport_' + type(exc).__name__) from None
        if 'Error' in result:
            raise RuntimeError('cloud_error_' + str(result['Error'].get('Code', 'unknown')))
        return result

    def describe(self):
        data = self.api('DescribeInstances', {'InstanceIds': [INSTANCE]})
        need(len(data.get('InstanceSet', [])) == 1, 'instance_not_unique')
        item = data['InstanceSet'][0]
        need(item['InstanceId'] == INSTANCE and PUBLIC_IP in item.get('PublicIpAddresses', []),
             'instance_identity_changed')
        return dict(instance_id=INSTANCE, public_ips=item['PublicIpAddresses'], region=REGION,
                    cap_mbps=item['InternetAccessible']['InternetMaxBandwidthOut'],
                    billing=item['InternetAccessible']['InternetChargeType'],
                    state=item['InstanceState'], operation=item.get('LatestOperation'),
                    operation_state=item.get('LatestOperationState'), request_id=data['RequestId'])

    def quote(self, cap):
        return self.api('InquiryPriceResetInstancesInternetMaxBandwidth', bandwidth(cap))


def bandwidth(cap):
    return {'InstanceIds': [INSTANCE], 'InternetAccessible': {'InternetMaxBandwidthOut': cap}}


def plan(cloud, *, rollback=False):
    current = cloud.describe()
    expected, target = ((AFTER_MBPS, BEFORE_MBPS) if rollback else (BEFORE_MBPS, AFTER_MBPS))
    need(current['state'] == 'RUNNING', 'instance_not_running')
    need(current['billing'] == BILLING, 'billing_mode_changed')
    need(current['cap_mbps'] == expected, 'bandwidth_changed_by_another_operator')
    old_quote, new_quote = cloud.quote(expected), cloud.quote(target)
    old_price = old_quote['Price']['BandwidthPrice']
    new_price = new_quote['Price']['BandwidthPrice']
    need(old_price.get('ChargeUnit') == 'GB' and new_price == old_price and new_quote['Price'] == old_quote['Price'],
         'traffic_tariff_changed_review_required')
    return dict(before=current, target_mbps=target, billing=BILLING,
                before_quote=old_quote, after_quote=new_quote,
                traffic_tariff_unchanged=True,
                note='Bandwidth cap changes; actual outbound GB remain billed at the same tariff.')


def observe(cloud, manifest, path, *, rollback=False):
    target = BEFORE_MBPS if rollback else AFTER_MBPS
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        current = cloud.describe()
        manifest['last_observation'] = current
        if current['cap_mbps'] == target and current.get('operation_state') == 'SUCCESS':
            manifest['phase'] = 'rolled_back' if rollback else 'confirmed'
            manifest['confirmed_at'] = datetime.now(timezone.utc).isoformat()
            atomic_json(path, manifest)
            return manifest
        atomic_json(path, manifest)
        time.sleep(2)
    manifest['phase'] = 'rollback_unconfirmed' if rollback else 'unconfirmed'
    atomic_json(path, manifest)
    return manifest


def change(cloud, directory, commit, *, rollback=False):
    path = directory / 'manifest.json'
    if path.exists():
        manifest = json.loads(path.read_text())
        need(manifest['instance_id'] == INSTANCE, 'manifest_instance_mismatch')
        # Retry only the readback after a missing/uncertain cloud response.
        if not rollback or manifest['phase'] in {'rollback_requested', 'rollback_unconfirmed', 'rolled_back'}:
            return observe(cloud, manifest, path, rollback=rollback)
        need(manifest['phase'] == 'confirmed', 'unconfirmed_change_preserved')
    else:
        need(not rollback, 'rollback_manifest_missing')
        directory.mkdir(parents=True, mode=0o700)
        manifest = dict(instance_id=INSTANCE, source_commit=commit,
                        created_at=datetime.now(timezone.utc).isoformat())
    reviewed = plan(cloud, rollback=rollback)
    manifest['rollback_plan' if rollback else 'plan'] = reviewed
    manifest['phase'] = 'rollback_requested' if rollback else 'requested'
    atomic_json(path, manifest)
    try:
        receipt = cloud.api('ResetInstancesInternetMaxBandwidth', bandwidth(reviewed['target_mbps']))
        manifest['rollback_request_id' if rollback else 'request_id'] = receipt['RequestId']
    except RuntimeError as exc:
        manifest['rollback_error' if rollback else 'error'] = str(exc)
        atomic_json(path, manifest)
        # No write retry or automatic reversal: cloud state may already have changed.
    atomic_json(path, manifest)
    return observe(cloud, manifest, path, rollback=rollback)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['plan', 'apply', 'rollback'])
    parser.add_argument('--deployment')
    args = parser.parse_args()
    cloud = Cloud()
    if args.mode == 'plan':
        print(json.dumps(plan(cloud), ensure_ascii=False, indent=2))
        return
    need(subprocess.check_output(['findmnt', '-no', 'UUID', '/mnt/data-disk'], text=True).strip() == UUID,
         'data_disk_identity_mismatch')
    need(args.deployment, 'deployment_path_required')
    directory = Path(args.deployment).resolve()
    need(DATA in directory.parents, 'deployment_outside_task_data')
    source = Path(__file__).resolve().parents[2]
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    need(not subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True).strip(),
         'source_not_clean')
    need(subprocess.check_output(['git', '-C', str(source), 'for-each-ref', '--contains', commit,
                                 '--format=%(refname)', 'refs/remotes/origin'], text=True).strip(),
         'commit_not_in_fetched_origin')
    result = change(cloud, directory, commit, rollback=args.mode == 'rollback')
    print(json.dumps({'phase': result['phase'], 'instance_id': INSTANCE, 'deployment': str(directory),
                      'observation': result.get('last_observation'), 'request_id': result.get('request_id')},
                     ensure_ascii=False, indent=2))
    if result['phase'] not in {'confirmed', 'rolled_back'}:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
