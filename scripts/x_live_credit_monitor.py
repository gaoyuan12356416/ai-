#!/usr/bin/env python3
"""Hourly CPU monitor for the X App actually loaded by the production backend."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

import x_app_credit_monitor as common

ROOT = Path('/mnt/data-disk/x-api-credit-monitor')
UNIT = 'x-post-automation.service'


class MonitorError(RuntimeError):
    pass


def live_environment():
    pid = int(subprocess.check_output(
        ['systemctl', 'show', UNIT, '--property=MainPID', '--value'], text=True, timeout=10))
    if pid <= 1:
        raise MonitorError('production_service_unavailable')
    fields = Path('/proc/%d/environ' % pid).read_bytes().split(b'\0')
    env = dict(field.decode().split('=', 1) for field in fields if b'=' in field)
    if not env.get('X_CLIENT_ID') or env.get('X_LISTEN_PORT') != '8810':
        raise MonitorError('production_configuration_unavailable')
    return env, pid


def epoch(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def select_account(rows, at):
    eligible = [r for r in rows if r['status'] == 'active' and r['publish_approved'] == 1]
    if not eligible:
        raise MonitorError('no_active_production_account')
    row = max(eligible, key=lambda r: (epoch(r['access_expires_at']), r['id']))
    return row, epoch(row['access_expires_at']) <= at + 120


def account_rows(env):
    path = Path(env['X_ACCOUNTS_DB']).resolve()
    if path != Path('/var/lib/x-post-automation/accounts.sqlite3').resolve():
        raise MonitorError('production_database_path_changed')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        return [dict(r) for r in conn.execute(
            'SELECT id,x_user_id,status,publish_approved,access_expires_at FROM x_authorized_account')]
    finally:
        conn.close()


def refresh_if_needed(env, row):
    """Delegate expired-token lifecycle to the running service and its own locks."""
    payload = {'actor': {'role': 'admin', 'user_id': 'x-api-credit-monitor',
                         'tenant_key': 'system', 'name': 'X balance monitor'},
               'scope': 'all', 'only_refresh_required': True, 'preserve_transient_status': True}
    req = urllib.request.Request('http://127.0.0.1:8810/internal/accounts/%s/verify' % row['id'],
        data=json.dumps(payload).encode(), headers={
            'Authorization': 'Bearer ' + env['X_INTERNAL_TOKEN'], 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), common.NoRedirect())
    try:
        with opener.open(req, timeout=90) as response:
            result = json.loads(response.read(131072))
    except urllib.error.HTTPError as exc:
        # Profile verification can fail after a successful refresh, e.g. no credits.
        # Do not rotate again or fail over to other accounts.
        raise MonitorError('production_token_verify_http_' + str(exc.code)) from None
    item = result.get('item') or {}
    if str(item.get('x_user_id')) != str(row['x_user_id']) or item.get('status') != 'active':
        raise MonitorError('production_token_identity_unavailable')


def production_balance(config):
    env, pid = live_environment()
    fingerprint = hashlib.sha256(env['X_CLIENT_ID'].encode()).hexdigest()
    if fingerprint != config['expected_client_id_sha256']:
        raise MonitorError('production_app_changed_rebind_required')
    row, refresh_required = select_account(account_rows(env), datetime.now(timezone.utc).timestamp())
    if refresh_required:
        refresh_if_needed(env, row)
    uid = str(row['x_user_id'])
    if not re.fullmatch(r'[0-9]{1,32}', uid):
        raise MonitorError('invalid_production_account_identity')
    directory = Path(env['X_TOKENS_DIR']).resolve()
    if directory != Path('/var/lib/x-post-automation/tokens').resolve():
        raise MonitorError('production_token_path_changed')
    path = directory / (uid + '.json')
    if path.is_symlink():
        raise MonitorError('unexpected_token_symlink')
    token = json.loads(path.read_text())['access_token']
    # Fail closed if a concurrent deployment changed the loaded application.
    after, after_pid = live_environment()
    if pid != after_pid or env['X_CLIENT_ID'] != after.get('X_CLIENT_ID'):
        raise MonitorError('production_app_changed_during_check')
    balance = common.fetch_balance(token)
    return balance, {'service': UNIT, 'pid': pid, 'account_id': row['id'],
                     'app_fingerprint': fingerprint, 'token_refreshed_if_required': refresh_required}


def live_message(balance, setup):
    checked = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    title = 'AI 后台线上 X 余额监控已启用' if setup else 'AI 后台线上 X 可用余额不足 5 美元'
    return (title + '\n可用总余额：$' + balance['total_balance']
            + '\n充值余额：$' + balance['prepaid_balance']
            + '\n赠送余额：$' + balance['free_balance']
            + '\n查询时间：' + checked + '（北京时间）'
            + '\n范围：AI 后台当前运行的 X App 所属开发者计费账户。'
            + '\nCPU 服务器每小时检查；低于 $5 提醒，同一次低余额只提醒一次。'
            + ('\n此前测试 App 的 Codex 自动任务已取消；本监控不依赖电脑或 Codex。' if setup else
               '\n请及时检查充值；恢复到 $5 或以上后，再次跌破会重新提醒。')
            + '\nhttps://console.x.com/\n来源：X GET /2/usage/credits')


def failure(state, code, *, persist, sender):
    count = state.get('consecutive_failures', 0) + 1
    state.update(last_attempt_at=common.now(), last_error=code, consecutive_failures=count)
    persist(state)
    if count < 3 and not code.startswith('production_app_changed'):
        return {'status': 'query_failed', 'consecutive_failures': count, 'error': code}
    record = state.setdefault('failure_alert', {'uuid': str(uuid.uuid4()), 'status': 'pending'})
    if record['status'] == 'sending':
        record['status'] = 'unknown'
    if record['status'] in ('pending', 'rejected'):
        record['status'] = 'sending'
        persist(state)
        try:
            record.update(sender('AI 后台线上 X 余额监控异常\n当前余额未知，不能视为 0。'
                + '\n错误：' + code + '\n连续失败次数：' + str(count)
                + '\n请检查服务器上的 x-api-credit-monitor.service。', record['uuid']))
        except Exception:
            record['status'] = 'unknown'
    persist(state)
    return {'status': 'query_failed', 'error': code, 'consecutive_failures': count,
            'failure_notification_status': record['status']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--notify', action='store_true')
    parser.add_argument('--send-setup', action='store_true')
    args = parser.parse_args()
    import fcntl
    os.umask(0o077)
    root = ROOT / 'data'
    root.mkdir(mode=0o700, exist_ok=True)
    with (root / 'monitor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads((ROOT / 'config.json').read_text())
        path = root / 'state.json'
        state = json.loads(path.read_text()) if path.exists() else {'version': 1, 'scope': 'production'}
        persist = lambda value: common.save(path, value)
        sender = lambda text, key: common.send_private(config['feishu'], text, key)
        try:
            balance, evidence = production_balance(config)
        except Exception as exc:
            code = str(exc) if isinstance(exc, MonitorError) else type(exc).__name__
            if args.notify or args.send_setup:
                result = failure(state, code, persist=persist, sender=sender)
            else:
                result = {'status': 'query_failed', 'error': code}
            print(json.dumps(result, ensure_ascii=False))
            return 2
        previous_failure = state.get('failure_alert')
        if previous_failure and previous_failure['status'] not in ('sending', 'unknown'):
            state['last_failure_alert'] = state.pop('failure_alert')
        state['source'] = evidence
        result = common.update(state, balance, notify=args.notify, setup=args.send_setup,
                               persist=persist, sender=sender, message_builder=live_message)
        print(json.dumps({**result, 'balance_usd': balance, 'source': evidence,
                          'checked_at': state['last_success_at']}, ensure_ascii=False))
        return 2 if result['status'] in ('delivery_unknown', 'notification_rejected') else 0


if __name__ == '__main__':
    sys.exit(main())
