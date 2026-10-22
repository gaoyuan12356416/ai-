#!/usr/bin/env python3
"""Production smoke checks with GET only; never creates tasks or invokes X.

The optional Cookie file must contain only the existing HTTP Cookie header value.
It is read into memory, never echoed, and never sent to redirected/foreign URLs.
Optional SQLite inspection uses mode=ro + query_only, without publisher imports.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request


ASSETS = (
    '/x-video-publish.html', '/x-video-publish.js', '/x-video-publish.css',
    '/x-video-account-settings.js', '/x-video-account-settings.css',
    '/x-excel-publish.js', '/x-account-list.html', '/vendor/xlsx.full.min.js',
    '/vendor/LICENSE.sheetjs.txt', '/templates/x-publishing-template.xlsx',
)
API_READS = ('/accounts', '/languages', '/materials?limit=1', '/batches?limit=1')
SENSITIVE_KEYS = {'access_token', 'refresh_token', 'token_store_key', 'credentials',
                  'owner_tenant_key', 'owner_user_id', 'permissions_json', 'session_token'}
LEDGER_TABLES = ('x_post_manual_run', 'x_post_queue', 'x_post_publish_log',
                 'x_video_publish_batch', 'x_video_publish_task', 'x_video_publish_pool',
                 'x_video_publish_pool_item', 'x_video_publish_pool_slot')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Getter:
    """Only relative, allowlisted GET requests, without proxies or redirects."""
    def __init__(self, base, cookie='', timeout=30):
        parts = urllib.parse.urlsplit(base)
        if (parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or parts.path not in {'', '/'}
                or (parts.scheme == 'http' and parts.hostname not in {'127.0.0.1', 'localhost', '::1'})):
            raise ValueError('Use an HTTPS origin or loopback HTTP origin')
        if any(ord(c) < 32 for c in cookie):
            raise ValueError('Cookie must be a single header value')
        self.base, self.cookie, self.timeout = base.rstrip('/'), cookie, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(self, path, *, authenticated=False):
        allowed = set(ASSETS) | {'/api/ui/topbar', '/api/x-video-publish/accounts',
            '/api/x-video-publish/languages', '/api/x-video-publish/materials?limit=1',
            '/api/x-video-publish/batches?limit=1', '/internal/x-video-publish/identity',
            '/internal/x-video-publish/source'}
        exact_material = re.fullmatch(r'/api/x-video-publish/materials\?limit=1&q=[1-9][0-9]{0,30}', path)
        if path not in allowed and not exact_material:
            raise ValueError('Path is outside the read-only verification allowlist')
        headers = {'Accept': '*/*', 'Cache-Control': 'no-cache', 'User-Agent': 'XPersonalReleaseReadOnly/1'}
        if authenticated and self.cookie:
            headers['Cookie'] = self.cookie
        request = urllib.request.Request(self.base + path, headers=headers, method='GET')
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError('Response exceeds verification limit')
            return response.status, dict(response.headers), body


def no_sensitive_keys(value):
    if isinstance(value, dict):
        if SENSITIVE_KEYS.intersection(value):
            return False
        return all(no_sensitive_keys(v) for v in value.values())
    if isinstance(value, list):
        return all(no_sensitive_keys(v) for v in value)
    return True


def dto_check(path, data):
    if not isinstance(data, dict) or not no_sensitive_keys(data) or not isinstance(data.get('items'), list):
        return False
    if path == '/accounts':
        required = {'account_id', 'x_user_id', 'display_name', 'language', 'eligible', 'caption_template', 'config_version'}
        return all(isinstance(row, dict) and required <= row.keys()
                   and isinstance(row['account_id'], str) and isinstance(row['x_user_id'], str)
                   and type(row['eligible']) is bool and type(row['config_version']) is int for row in data['items'])
    if path == '/languages':
        return bool(data['items']) and all(isinstance(row, dict) and {'value', 'label'} <= row.keys() for row in data['items'])
    if path.startswith('/materials'):
        required = {'material_id', 'material_name', 'drama_name', 'language', 'duration_seconds', 'thumbnail_url', 'preview_url'}
        return 'next_cursor' in data and all(isinstance(row, dict) and required <= row.keys()
            and isinstance(row['material_id'], str) for row in data['items'])
    if path.startswith('/batches'):
        return 'next_cursor' in data and all(isinstance(row, dict) and
            {'batch_id', 'source', 'status', 'created_at_utc', 'total_tasks'} <= row.keys()
            and isinstance(row['batch_id'], str) for row in data['items'])
    return False


def inspect_database(path):
    """Read counts and state summaries; do not instantiate store migrations."""
    resolved = path.resolve(strict=True)
    uri = resolved.as_uri() + '?mode=ro'
    result = {}
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in LEDGER_TABLES:
            if table not in existing:
                result[table] = {'present': False}
                continue
            columns = {r[1] for r in conn.execute('PRAGMA table_info("' + table + '")')}
            item = {'present': True, 'count': conn.execute('SELECT COUNT(*) FROM "'+table+'"').fetchone()[0]}
            if 'id' in columns:
                item['max_id'] = conn.execute('SELECT MAX(id) FROM "'+table+'"').fetchone()[0]
            if 'status' in columns:
                item['statuses'] = dict(conn.execute('SELECT status, COUNT(*) FROM "'+table+'" GROUP BY status'))
            if table == 'x_post_publish_log' and {'x_post_id', 'unknown_outcome'} <= columns:
                item['confirmed_posts'] = conn.execute('SELECT COUNT(*) FROM x_post_publish_log WHERE '
                    "status='published' AND x_post_id IS NOT NULL AND unknown_outcome=0").fetchone()[0]
                item['unknown_outcomes'] = conn.execute('SELECT COUNT(*) FROM x_post_publish_log WHERE unknown_outcome=1').fetchone()[0]
            result[table] = item
        conn.rollback()
    return result


def verify(getter, *, source_root=None, authenticated=False, material_id=None):
    checks = []
    def record(name, ok, **info):
        checks.append({'name': name, 'ok': bool(ok), **info})
    for path in ASSETS:
        status, headers, body = getter.get(path)
        digest = hashlib.sha256(body).hexdigest()
        ok = status == 200 and bool(body)
        if path.endswith('.xlsx'):
            ok = ok and body.startswith(b'PK')
        elif path.endswith('.js') or path.endswith('.css') or path.endswith('.txt'):
            ok = ok and not body.lstrip().lower().startswith((b'<!doctype', b'<html'))
        if path == '/x-account-list.html':
            header = re.search(br'<thead>(.*?)</thead>', body, re.S)
            ok = ok and header is not None and len(re.findall(br'<th(?:\s|>)', header.group(1))) == 8
        if path == '/x-video-publish.html':
            ok = ok and b'/x-video-publish.js' in body and b'/ui-topbar.js' in body and b'/quick-nav.js' in body
        if source_root:
            local = source_root / 'static' / path.lstrip('/')
            ok = ok and local.is_file() and hashlib.sha256(local.read_bytes()).hexdigest() == digest
        record('asset '+path, ok, status=status, sha256=digest, bytes=len(body))
    status, _, body = getter.get('/api/x-video-publish/accounts')
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        data = {}
    record('cookie required', status in {401, 403} and data.get('code') == 'cookie_auth_required', status=status)
    for path in ('/internal/x-video-publish/identity', '/internal/x-video-publish/source'):
        status, _, body = getter.get(path)
        record('internal route not public '+path, status in {401, 403, 404, 405}, status=status)
    if authenticated:
        paths = [*API_READS]
        if material_id:
            paths.append('/materials?limit=1&q='+material_id)
        for path in paths:
            status, headers, body = getter.get('/api/x-video-publish'+path, authenticated=True)
            try:
                data = json.loads(body)
            except (ValueError, UnicodeError):
                data = {}
            exact_ok = not (material_id and path.endswith('&q='+material_id)) or (
                len(data.get('items', [])) == 1 and data['items'][0].get('material_id') == material_id)
            record('authenticated GET '+path, status == 200 and dto_check(path, data) and exact_ok, status=status,
                   item_count=len(data.get('items', [])) if isinstance(data.get('items'), list) else None,
                   error_code=data.get('code') if status != 200 else None)
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='https://ai.yingliangads.com')
    parser.add_argument('--cookie-file', type=Path, help='Existing cookie header; never printed')
    parser.add_argument('--source-root', type=Path, help='Compare public new assets to this deployed source checkout')
    parser.add_argument('--material-id', help='Optional approved existing video ID for an exact read-only lookup')
    parser.add_argument('--database', type=Path, help='Optional read-only publisher DB count/state summary')
    parser.add_argument('--output', type=Path, help='Write safe verification JSON; parent must already exist')
    parser.add_argument('--database-only', action='store_true', help='No HTTP; requires --database')
    args = parser.parse_args(argv)
    if args.database_only and not args.database:
        parser.error('--database-only requires --database')
    if args.material_id and (not args.cookie_file or not re.fullmatch(r'[1-9][0-9]{0,30}', args.material_id)):
        parser.error('--material-id requires --cookie-file and a positive decimal ID string')
    result = {'at_utc': datetime.now(timezone.utc).isoformat(), 'http_method': 'GET',
              'verifier_post_requests': 0, 'verifier_x_api_requests': 0, 'checks': []}
    try:
        if not args.database_only:
            cookie = args.cookie_file.read_text(encoding='utf-8').strip() if args.cookie_file else ''
            if args.cookie_file and not cookie:
                raise ValueError('Cookie file is empty')
            result['checks'] = verify(Getter(args.base_url, cookie), source_root=args.source_root,
                                      authenticated=bool(cookie), material_id=args.material_id)
        if args.database:
            result['ledger'] = inspect_database(args.database)
        result['ok'] = all(item['ok'] for item in result['checks'])
    except Exception as exc:
        # Never echo exception text, request headers or response bodies.
        result['ok'], result['error_type'] = False, type(exc).__name__
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized+'\n', encoding='utf-8')
    print(serialized)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
