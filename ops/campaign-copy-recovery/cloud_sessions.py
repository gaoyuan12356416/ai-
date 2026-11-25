"""Collect read-only Tencent session receipts for the configured Campaign cluster."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import time
import urllib.request


CLUSTER = 'cynosdbmysql-5kxxsre7'
REGION = 'ap-hongkong'
READ_APIS = {'DescribeInstances': ('cynosdb', '2019-01-07'),
             'DescribeMySqlProcessList': ('dbbrain', '2021-05-27')}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def cloud_read(action, args):
    service, version = READ_APIS[action]
    cred = json.loads(Path('/root/.config/tencentcloud-billing/credentials.json').read_bytes())
    host = service + '.tencentcloudapi.com'
    stamp = int(time.time())
    day = datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d')
    body = json.dumps(args, separators=(',', ':')).encode()
    signed = 'content-type;host'
    headers = 'content-type:application/json; charset=utf-8\nhost:' + host + '\n'
    canonical = '\n'.join(('POST', '/', '', headers, signed, hashlib.sha256(body).hexdigest()))
    scope = '/'.join((day, service, 'tc3_request'))
    message = '\n'.join(('TC3-HMAC-SHA256', str(stamp), scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = ('TC3' + cred['SecretKey']).encode()
    for piece in (day, service, 'tc3_request'):
        key = hmac.new(key, piece.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    auth = 'TC3-HMAC-SHA256 Credential={}/{}, SignedHeaders={}, Signature={}'.format(cred['SecretId'], scope, signed, signature)
    request_headers = {'Content-Type': 'application/json; charset=utf-8', 'Host': host, 'Authorization': auth,
                       'X-TC-Action': action, 'X-TC-Version': version, 'X-TC-Region': REGION, 'X-TC-Timestamp': str(stamp)}
    if cred.get('Token'):
        request_headers['X-TC-Token'] = cred['Token']
    request = urllib.request.Request('https://' + host, data=body, headers=request_headers)
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=9) as stream:
        result = json.load(stream)['Response']
    if 'Error' in result:
        raise RuntimeError(action + ': ' + result['Error']['Code'])
    return result


def pending_login(row):
    user = str(row.get('User') or '').lower()
    command = str(row.get('Command') or '').lower()
    state = str(row.get('State') or '').lower()
    return (user in ('', 'unauthenticated', 'unauthenticated user') or 'auth' in state or 'login' in state
            or (command in ('', 'connect', 'connect out') and user not in ('system user', 'event_scheduler')))


def observe_cluster(incident, witness_id, user, call=cloud_read):
    start = time.time()
    cut = int((start - incident) * 0.97)
    if cut < 1 or witness_id < 1:
        raise ValueError('invalid handshake time or controller identity')
    inventory = call('DescribeInstances', {'Limit': 100})
    members = [r for r in inventory['InstanceSet'] if r['ClusterId'] == CLUSTER]
    if len(inventory['InstanceSet']) != inventory['TotalCount'] or not members or any(m['Status'] != 'running' for m in members):
        raise ValueError('cluster inventory is incomplete')
    nodes = []
    for member in members:
        node = {'instance': member['InstanceId'], 'role': member['InstanceRole']}
        base = {'Product': 'cynosdb', 'InstanceId': node['instance']}
        candidates = set()
        for label, filters in [('old', {'Time': cut}), ('preauth', {'User': 'unauthenticated user'}),
                               ('connecting', {'Command': 'Connect'}), ('witness', {'ID': witness_id, 'User': user})]:
            limit = 100 if label == 'old' else 40
            response = call('DescribeMySqlProcessList', dict(base, Limit=limit, **filters))
            rows = response['ProcessList']
            if not isinstance(rows, list) or len(rows) >= limit:
                raise ValueError('managed session list exceeds observation limit')
            if label == 'old' and any(int(r['Time']) < cut for r in rows):
                raise ValueError('age filter not honored')
            positive = []
            suspect = []
            for row in rows:
                thread = int(row['ID'])
                if label == 'witness':
                    if thread != witness_id or row['User'] != user:
                        raise ValueError('unexpected controller identity')
                    positive.append(dict(id=thread, user=user, host=row['Host']))
                elif pending_login(row):
                    suspect.append({'thread': thread})
                    candidates.add(thread)
            node[label] = dict(rows=len(rows), limit=limit, receipt=response['RequestId'],
                               wrong_time_filter=False, possible_old_preauth=suspect, positive=positive)
        if len(candidates) > 5:
            raise ValueError('too many ambiguous logins')
        node['resolved'] = {}
        for thread in sorted(candidates):
            response = call('DescribeMySqlProcessList', dict(base, ID=thread, Limit=2))
            rows = response['ProcessList']
            if not isinstance(rows, list) or len(rows) > 1 or any(int(r['ID']) != thread or pending_login(r) for r in rows):
                raise ValueError('previous login may still be present')
            node['resolved'][str(thread)] = dict(gone=not rows, authenticated=bool(rows), request=response['RequestId'])
        nodes.append(node)
    return dict(started_at=start, collected=time.time(), incident=incident, cut=cut, region=REGION, cluster=CLUSTER,
                inventory_receipt=inventory['RequestId'], member_ids=sorted(m['InstanceId'] for m in members), nodes=nodes)
