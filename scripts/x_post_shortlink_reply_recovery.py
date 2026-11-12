#!/usr/bin/env python3
"""Explicit one-time missing-link reply recovery; no scheduler or source-ledger writes.

Audit freezes both publication ledgers. Run needs the existing sidecar environment,
the service UID, an authorized administrator, and a separate durable receipt ledger.
Unknown writes are read back only, never replayed. No credentials enter artifacts.
"""
import argparse
import collections
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PREFIX = "Watch Full Now 👉"
FIELDS = "author_id,created_at,referenced_tweets,entities"
API = "https://api.x.com"


def now():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def emit(kind, **values):
    print(json.dumps(dict(event=kind, **values), ensure_ascii=False), flush=True)


def ro(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def has_link(text):
    return bool(re.search(r'https?://(?:gy\.g2flow\.com|t\.co)/', text, re.I))


def reply_text(url):
    if not re.fullmatch(r'https://gy\.g2flow\.com/(?:s/[1-9][0-9]*|s2l(?:-tag)?/[1-9][0-9]*\.html)', url):
        raise ValueError('invalid_short_url')
    text = PREFIX + url
    if any(x in text for x in ('{', '}', '\n', '\r')):
        raise ValueError('unexpanded_macro')
    return text


def redirect_target(path):
    raw = Path(path).read_text(encoding='utf-8')
    m = re.search(r'location\.replace\(("(?:\\.|[^"\\])*")\);', raw)
    if not m:
        raise ValueError('redirect_target_missing')
    return json.loads(m.group(1))


def validate_link(row, roots, shared_root):
    reply_text(row['short_url'])
    path = urllib.parse.urlsplit(row['short_url']).path
    if path.startswith('/s/'):
        short_id = int(path.split('/')[-1])
        with contextlib.closing(ro(Path(shared_root) / 'links.sqlite3')) as c:
            mapping = c.execute('SELECT * FROM short_link WHERE id=?', (short_id,)).fetchone()
        expected_origin = 'x_tag' if row['lane'] == 'tag' else 'x'
        if not mapping or mapping['origin'] != expected_origin or mapping['log_id'] != row['log_id'] or mapping['long_url'] != row['long_url']:
            raise ValueError('short_mapping_mismatch')
        target_file = Path(shared_root) / 'public' / (str(short_id) + '.html')
    else:
        expected = ('/s2l-tag/' if row['lane'] == 'tag' else '/s2l/') + str(row['log_id']) + '.html'
        if path != expected:
            raise ValueError('legacy_short_identity_mismatch')
        target_file = Path(roots[row['lane']]) / (str(row['log_id']) + '.html')
    if redirect_target(target_file) != row['long_url']:
        raise ValueError('redirect_target_mismatch')


def ledger(path):
    c = sqlite3.connect(path, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA synchronous=FULL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS reply(
      post_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, username TEXT NOT NULL,
      short_url TEXT NOT NULL, text TEXT NOT NULL, source_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'ready', attempts INTEGER NOT NULL DEFAULT 0,
      reply_id TEXT NOT NULL DEFAULT '', reply_url TEXT NOT NULL DEFAULT '',
      verified INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
      started_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS event(
      id INTEGER PRIMARY KEY, post_id TEXT, kind TEXT NOT NULL,
      detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
    ''')
    return c


def event(c, post_id, kind, **detail):
    c.execute('INSERT INTO event(post_id,kind,detail_json,created_at) VALUES(?,?,?,?)',
              (post_id, kind, json.dumps(detail, ensure_ascii=False), now()))


def audit(args):
    state = Path(args.state)
    state.parent.mkdir(parents=True, exist_ok=True)
    c = ledger(state)
    if c.execute('SELECT 1 FROM metadata').fetchone():
        raise ValueError('audit_already_frozen')
    sources = dict(standard=args.standard_db, tag=args.tag_db)
    roots = dict(standard=args.standard_public, tag=args.tag_public)
    all_rows, counts = {}, {}
    for lane, path in sources.items():
        with contextlib.closing(ro(path)) as src:
            rows = [dict(r) for r in src.execute('''
            SELECT l.id log_id,l.queue_id,l.account_id,l.published_at,l.short_url,l.long_url,
                   l.post_text,l.x_post_id,l.x_post_url,q.account_username,q.drama_name,
                   q.content_id,q.delivery_mode,q.body_template
            FROM x_post_publish_log l JOIN x_post_queue q ON q.id=l.queue_id
            WHERE l.status='published' AND l.published_at>=? AND l.published_at<?
            ORDER BY l.published_at,l.id''', (args.start, args.end))]
        counts[lane] = dict(published=len(rows), missing=sum(not has_link(r['post_text']) for r in rows))
        for row in rows:
            pid = row['x_post_id']
            if not re.fullmatch('[0-9]+', pid):
                raise ValueError('confirmed_post_missing_id')
            if pid in all_rows and all_rows[pid]['short_url'] != row['short_url']:
                raise ValueError('duplicate_post_conflicting_links')
            row.update(lane=lane, source_db=path)
            all_rows[pid] = row
    missing = [r for r in all_rows.values() if not has_link(r['post_text'])]
    for row in missing:
        if row['delivery_mode'] != 'direct':
            raise ValueError('relay_requires_author_resolution')
        validate_link(row, roots, args.shared_root)
        c.execute('''INSERT INTO reply(post_id,account_id,username,short_url,text,source_json,updated_at)
                     VALUES(?,?,?,?,?,?,?)''',
                  (row['x_post_id'], row['account_id'], row['account_username'], row['short_url'],
                   reply_text(row['short_url']), json.dumps(row, ensure_ascii=False), now()))
    summary = dict(start=args.start, end=args.end, lanes=counts, published=len(all_rows),
                   missing=len(missing), accounts=len(set(r['account_id'] for r in missing)),
                   day_counts=dict(collections.Counter((dt.datetime.fromisoformat(r['published_at'].replace('Z','+00:00')) + dt.timedelta(hours=8)).date().isoformat() for r in missing)))
    for key, value in [('audit', summary), ('roots', roots), ('shared_root', args.shared_root)]:
        c.execute('INSERT INTO metadata VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))
    event(c, '', 'audit_frozen', **summary)
    c.commit()
    emit('audit', **summary)


class ApiError(Exception):
    def __init__(self, status, detail, uncertain=False):
        super().__init__(detail)
        self.status, self.uncertain = status, uncertain


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api(token, method, path, payload=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=None if payload is None else json.dumps(payload).encode('utf-8'),
                                 headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=35) as r:
            raw = r.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('response_too_large')
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read(8192).decode('utf-8', 'replace')
        try:
            info = json.loads(raw)
            detail = json.dumps({k:info[k] for k in ['title','detail','type','errors'] if k in info}, ensure_ascii=False)[:1200]
        except ValueError:
            detail = 'upstream_http_error'
        raise ApiError(exc.code, detail, method == 'POST' and exc.code >= 500) from None
    except (OSError, ValueError) as exc:
        raise ApiError(0, type(exc).__name__, method == 'POST') from None


def expanded_text(post):
    text = post.get('text', '')
    for url in post.get('entities', {}).get('urls', []):
        text = text.replace(url.get('url', '') or '\0', url.get('expanded_url', '') or url.get('url', ''))
    return text


def is_reply(post, parent, user_id, text):
    return (str(post.get('author_id')) == str(user_id)
            and any(r.get('type') == 'replied_to' and str(r.get('id')) == str(parent)
                    for r in post.get('referenced_tweets', []))
            and expanded_text(post) == text)


def mark(c, row, status, **fields):
    permitted = {'reply_id','reply_url','verified','error','started_at'}
    if set(fields) - permitted:
        raise ValueError('invalid_status_fields')
    fields.update(status=status, updated_at=now())
    c.execute('UPDATE reply SET '+','.join(k+'=?' for k in fields)+' WHERE post_id=?', list(fields.values())+[row['post_id']])
    event(c, row['post_id'], status, **{k:v for k,v in fields.items() if k != 'status'})
    c.commit()


def run(args):
    import fcntl
    if os.geteuid() == 0:
        raise ValueError('run_as_service_user_required')
    lock = open(str(args.state)+'.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    sys.path.insert(0, args.runtime_root)
    from features.x_accounts import oauth_service as oauth
    from features.x_accounts import client
    actor = dict(user_id=args.actor_user, tenant_key=args.actor_tenant, name=args.actor_name, role='admin')
    client.configure_x_accounts_client(args.sidecar_url, oauth.INTERNAL_TOKEN, timeout=60)
    c = ledger(args.state)
    meta = {r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM metadata')}
    rows = [dict(r) for r in c.execute("SELECT * FROM reply WHERE status IN ('ready','sending','unknown') ORDER BY account_id,post_id")]
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[row['account_id']].append(row)
    sent, attempted = 0, 0
    for aid, candidates in grouped.items():
        if args.limit and attempted >= args.limit:
            break
        try:
            client.verify_x_account(aid, actor, 'all', only_refresh_required=True, preserve_transient_status=True)
            with oauth.publish_credentials(aid, actor, 'all') as (account, token):
                user_id = account['x_user_id']
                timeline, cursor = [], ''
                for _ in range(20):
                    params = {'start_time':meta['audit']['start'], 'max_results':100,
                              'tweet.fields':FIELDS, 'exclude':'retweets'}
                    if cursor:
                        params['pagination_token'] = cursor
                    result = api(token, 'GET', '/2/users/'+user_id+'/tweets?'+urllib.parse.urlencode(params))
                    if result.get('errors'):
                        raise ApiError(200, 'incomplete_timeline')
                    timeline.extend(result.get('data', []))
                    cursor = result.get('meta', {}).get('next_token', '')
                    if not cursor:
                        break
                if cursor:
                    raise ApiError(200, 'timeline_page_limit')
        except Exception as exc:
            code = getattr(exc, 'code', type(exc).__name__)
            for row in candidates:
                if row['status'] == 'ready':
                    mark(c, row, 'preflight_failed', error=str(code)+': '+str(exc)[:1200])
            emit('account_preflight_failed', account_id=aid, error=str(code), detail=str(exc)[:1200])
            continue
        originals = {str(p['id']):p for p in timeline}
        for row in candidates:
            if args.limit and attempted >= args.limit:
                break
            existing = [p for p in timeline if is_reply(p, row['post_id'], user_id, row['text'])]
            if existing:
                p = existing[0]
                mark(c, row, 'existing', reply_id=p['id'], reply_url='https://x.com/'+account['username']+'/status/'+p['id'], verified=1)
                emit('existing', post_id=row['post_id'], reply_id=p['id'])
                continue
            if row['status'] in ('sending','unknown'):
                emit('unknown_preserved', post_id=row['post_id'])
                continue
            try:
                frozen = json.loads(row['source_json'])
                with contextlib.closing(ro(frozen['source_db'])) as src:
                    current = src.execute('SELECT * FROM x_post_publish_log WHERE id=?', (frozen['log_id'],)).fetchone()
                if not current or current['status'] != 'published' or any(current[k] != frozen[k] for k in ['x_post_id','short_url','long_url','post_text','account_id']):
                    raise ValueError('source_changed')
                validate_link(frozen, meta['roots'], meta['shared_root'])
                if row['text'] != reply_text(row['short_url']):
                    raise ValueError('reply_text_changed')
                parent = originals.get(row['post_id'])
                if not parent:
                    with oauth.publish_credentials(aid, actor, 'all') as (_, token):
                        result = api(token, 'GET', '/2/tweets/'+row['post_id']+'?'+urllib.parse.urlencode({'tweet.fields':FIELDS}))
                    parent = result.get('data')
                if not parent or str(parent.get('author_id')) != user_id:
                    raise ValueError('parent_missing_or_author_mismatch')
                if has_link(expanded_text(parent)):
                    mark(c, row, 'already_in_body', verified=1)
                    continue
                client.verify_x_account(aid, actor, 'all', only_refresh_required=True, preserve_transient_status=True)
            except Exception as exc:
                mark(c, row, 'preflight_failed', error=str(getattr(exc,'code',type(exc).__name__))+': '+str(exc)[:1000])
                emit('preflight_failed', post_id=row['post_id'], error=str(exc)[:1000])
                continue
            try:
                with oauth.publish_credentials(aid, actor, 'all') as (account, token):
                    if account['x_user_id'] != user_id:
                        raise ValueError('identity_changed')
                    with c:
                        n = c.execute("UPDATE reply SET status='sending',attempts=attempts+1,started_at=?,updated_at=? WHERE post_id=? AND status='ready' AND attempts=0", (now(),now(),row['post_id'])).rowcount
                        if n != 1:
                            raise ValueError('already_attempted')
                        event(c, row['post_id'], 'request_reserved', text=row['text'])
                    attempted += 1
                    result = api(token, 'POST', '/2/tweets', {'text':row['text'], 'reply':{'in_reply_to_tweet_id':row['post_id']}})
                    reply_id = str(result.get('data', {}).get('id', ''))
                    if not re.fullmatch('[0-9]+', reply_id):
                        raise ApiError(201, 'missing_created_reply_id', True)
                    mark(c, row, 'published', reply_id=reply_id, reply_url='https://x.com/'+account['username']+'/status/'+reply_id)
                    sent += 1
                    try:
                        proof = api(token, 'GET', '/2/tweets/'+reply_id+'?'+urllib.parse.urlencode({'tweet.fields':FIELDS})).get('data', {})
                        if not is_reply(proof, row['post_id'], user_id, row['text']):
                            raise ValueError('reply_readback_mismatch')
                        mark(c, row, 'published', verified=1)
                    except Exception as exc:
                        mark(c, row, 'published', error='readback: '+str(exc)[:1000])
                    emit('published', post_id=row['post_id'], reply_id=reply_id, sent=sent)
            except Exception as exc:
                state = c.execute('SELECT status FROM reply WHERE post_id=?', (row['post_id'],)).fetchone()[0]
                if state == 'published':
                    raise
                unknown = state == 'sending' and (not isinstance(exc, ApiError) or exc.uncertain)
                status = 'unknown' if unknown else ('rejected' if isinstance(exc, ApiError) else 'preflight_failed')
                mark(c, row, status, error=str(exc)[:1200])
                emit(status, post_id=row['post_id'], http_status=getattr(exc,'status',0), error=str(exc)[:1200])
                if unknown or getattr(exc,'status',0) in (401,429):
                    break
            time.sleep(1)
        emit('account_complete', account_id=aid)
    report(args)


def report(args):
    with contextlib.closing(ro(args.state)) as c:
        summary = json.loads(c.execute("SELECT value FROM metadata WHERE key='audit'").fetchone()[0])
        summary['statuses'] = dict(c.execute('SELECT status,count(*) FROM reply GROUP BY status').fetchall())
        summary['verified'] = c.execute('SELECT count(*) FROM reply WHERE verified=1').fetchone()[0]
        summary['attempts'] = c.execute('SELECT sum(attempts) FROM reply').fetchone()[0]
        rows = [dict(r) for r in c.execute('SELECT * FROM reply ORDER BY account_id,post_id')]
    if args.output:
        Path(args.output).write_text(json.dumps({'summary':summary,'rows':rows}, ensure_ascii=False, indent=2), encoding='utf-8')
    emit('summary', **summary)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['audit','run','report'])
    p.add_argument('--state', required=True)
    p.add_argument('--start')
    p.add_argument('--end')
    p.add_argument('--standard-db', default='/var/lib/x-post-automation/accounts.sqlite3')
    p.add_argument('--tag-db', default='/mnt/data-disk/x-tag-publisher/posts.sqlite3')
    p.add_argument('--standard-public', default='/var/lib/x-post-automation/s2l')
    p.add_argument('--tag-public', default='/mnt/data-disk/x-tag-publisher/s2l')
    p.add_argument('--shared-root', default='/mnt/data-disk/x-shared-short-links')
    p.add_argument('--runtime-root', default='/opt/x-post-automation/current')
    p.add_argument('--sidecar-url', default='http://127.0.0.1:8810')
    p.add_argument('--actor-user')
    p.add_argument('--actor-tenant')
    p.add_argument('--actor-name', default='Authorized operator')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--output')
    args = p.parse_args()
    if args.action == 'audit' and (not args.start or not args.end or args.start >= args.end):
        p.error('audit requires ordered UTC --start and --end')
    if args.action == 'run' and (not args.actor_user or not args.actor_tenant):
        p.error('run requires an authorized administrator actor')
    globals()[args.action](args)


if __name__ == '__main__':
    main()
