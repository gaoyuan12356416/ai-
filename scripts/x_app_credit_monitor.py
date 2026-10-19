#!/usr/bin/env python3
"""Read the new X App's USD credit balance; notify one private Feishu recipient.

Invoked by the chat heartbeat. Does not publish Posts or touch publisher state.
"""
import argparse
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid

import x_credit_feishu_transport as feishu

DATA = Path('/mnt/data-disk/x-new-app-sandbox-20261019/data')
THRESHOLD = Decimal('5')


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def parse_balance(payload):
    if payload.get('errors') or not isinstance(payload.get('data'), dict):
        raise ValueError('X balance missing or partial error')
    result = {}
    for key in ('total_balance', 'prepaid_balance', 'free_balance'):
        raw = payload['data'].get(key)
        if isinstance(raw, bool) or raw is None:
            raise ValueError('X balance invalid')
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            raise ValueError('X balance invalid') from None
        if not value.is_finite() or (key != 'prepaid_balance' and value < 0):
            raise ValueError('X balance invalid')
        result[key] = str(value)
    expected = max(Decimal('0'), Decimal(result['prepaid_balance']) + Decimal(result['free_balance']))
    if abs(Decimal(result['total_balance']) - expected) > Decimal('0.000001'):
        raise ValueError('X balance components inconsistent')
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_balance(token):
    req = urllib.request.Request('https://api.x.com/2/usage/credits', headers={
        'Authorization': 'Bearer ' + token, 'User-Agent': 'yingliang-x-credit-monitor/1.0'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=25) as response:
            payload = json.loads(response.read(65536), parse_float=Decimal)
    except urllib.error.HTTPError as exc:
        raise RuntimeError('X balance HTTP ' + str(exc.code)) from None
    return parse_balance(payload)


def send_private(config, text, message_uuid):
    if config.get('receive_id_type') != 'open_id' or config.get('recipient_name') != '郜远':
        raise ValueError('Private recipient guard failed')
    feishu.APP_ID, feishu.APP_SECRET = config['app_id'], config['app_secret']
    token = feishu.get_tenant_access_token()
    payload = {'receive_id': config['receive_id'], 'msg_type': 'text',
               'content': json.dumps({'text': text}, ensure_ascii=False), 'uuid': message_uuid}
    result = feishu.post_json(feishu.BASE_URL + '/im/v1/messages?receive_id_type=open_id', payload, token)
    if result.get('code') != 0:
        return {'status': 'rejected', 'code': result.get('code')}
    message_id = (result.get('data') or {}).get('message_id')
    if not message_id:
        raise RuntimeError('Feishu accepted without message receipt')
    return {'status': 'sent', 'message_id': message_id}


def message(balance, setup):
    title = 'X 新 App 余额提醒已启用' if setup else 'X 新 App 可用余额不足 5 美元'
    checked = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    return (title + '\n'
            '可用总余额：$' + balance['total_balance'] + '\n'
            '充值余额：$' + balance['prepaid_balance'] + '\n'
            '赠送余额：$' + balance['free_balance'] + '\n'
            '查询时间：' + checked + '（北京时间）\n'
            '范围：新 App 所属 X 开发者计费账户。\n'
            + ('每小时检查，严格低于 $5 时提醒；同一次低余额只提醒一次。\n'
               '这是配置成功通知，当前余额未触发低余额告警。\n' if setup else
               '请及时检查充值；充值恢复至 $5 或以上后，再次跌破会重新提醒。\n')
            + 'https://console.x.com/\n来源：X GET /2/usage/credits')


def update(state, balance, *, notify, setup, persist, sender, message_builder=message):
    """Persist intent before send. An ambiguous send is held for reconciliation."""
    state.update(last_success_at=now(), last_balance=balance, consecutive_failures=0, last_error=None)
    low = Decimal(balance['total_balance']) < THRESHOLD
    pending = state.get('episode')
    if not low and pending and pending.get('status') not in ('sending', 'unknown'):
        state['episode'] = None
    if low and not state.get('episode'):
        state['episode'] = {'uuid': str(uuid.uuid4()), 'status': 'pending', 'created_at': now()}
    persist(state)
    kind = 'setup' if setup and not low else 'episode'
    if setup and not low and not state.get('setup'):
        state['setup'] = {'uuid': str(uuid.uuid4()), 'status': 'pending', 'created_at': now()}
    record = state.get(kind)
    if record and record['status'] == 'sending':
        record['status'] = 'unknown'
        persist(state)
    if record and record['status'] == 'unknown':
        return {'status': 'delivery_unknown', 'low_balance': low}
    if (notify or setup) and record and record['status'] in ('pending', 'rejected') and (low or setup):
        record.update(status='sending', attempted_at=now())
        persist(state)
        try:
            receipt = sender(message_builder(balance, kind == 'setup'), record['uuid'])
        except Exception:
            record.update(status='unknown', error='notification_outcome_unknown')
            persist(state)
            return {'status': 'delivery_unknown', 'low_balance': low}
        record.update(receipt, completed_at=now())
        persist(state)
        return {**receipt, 'status': 'notification_' + receipt['status'], 'low_balance': low}
    return {'status': 'low_already_notified' if low and record and record['status'] == 'sent'
            else 'low_pending' if low else 'healthy', 'low_balance': low}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--notify', action='store_true')
    parser.add_argument('--send-setup', action='store_true')
    args = parser.parse_args()
    import fcntl
    os.umask(0o077)
    root = DATA / 'credit-monitor'
    root.mkdir(mode=0o700, exist_ok=True)
    with (root / 'monitor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / 'state.json'
        state = json.loads(path.read_text()) if path.exists() else {'version': 1}
        try:
            credentials = json.loads((DATA / 'credentials.json').read_text())
            fingerprint = hashlib.sha256(credentials['consumer_key'].encode()).hexdigest()
            if state.get('app_fingerprint', fingerprint) != fingerprint:
                raise ValueError('Monitored App changed')
            state['app_fingerprint'] = fingerprint
            balance = fetch_balance(credentials['bearer_token'])
        except Exception as exc:
            # Do not persist raw upstream responses or exception text containing secrets.
            state.update(last_attempt_at=now(), last_error=type(exc).__name__,
                         consecutive_failures=state.get('consecutive_failures', 0) + 1)
            save(path, state)
            print(json.dumps({'status': 'balance_query_failed', 'error_type': type(exc).__name__,
                              'consecutive_failures': state['consecutive_failures'],
                              'last_success_at': state.get('last_success_at')}))
            return 2
        config = json.loads((root / 'feishu.json').read_text()) if args.notify or args.send_setup else {}
        result = update(state, balance, notify=args.notify, setup=args.send_setup,
                        persist=lambda value: save(path, value),
                        sender=lambda text, key: send_private(config, text, key))
        print(json.dumps({**result, 'balance_usd': balance, 'checked_at': state['last_success_at']}, ensure_ascii=False))
        return 2 if result['status'] in ('delivery_unknown', 'notification_rejected') else 0


if __name__ == '__main__':
    sys.exit(main())
