"""Read-only TikTok 2250 attribution from public receipts and immutable code routes."""
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from features.youtube_analytics.report import ReportError
from .adapters import normalized, rows, snapshot, utc_day


POST_DB = '/mnt/data-disk/tt-post-publisher/tt-post.sqlite3'
AUTO_DB = '/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3'
USERS_DB = '/root/drama_material_service/data/drama_material_jobs.sqlite3'
# The publisher reserves this offset in the shared tt_post_code_route ledger.
AUTO_QUEUE_ID_NAMESPACE = 7_000_000_000_000_000_000


def campaign_key(campaign_id, campaign_name):
    """Both source columns are case-sensitive. The bounded key fits VARBINARY(1024)."""
    values = ['' if v is None else str(v) for v in (campaign_id, campaign_name)]
    payload = json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return 'tt:' + hashlib.sha256(payload).hexdigest()


def _route_key(route, task):
    if not route:
        return None
    expected = dict(af_channel='TT', af_c_id=str(task['id']), af_dp=str(task['content_id']),
                    af_adset_id=str(task['account_id']), af_ad_id=str(task['material_id']), c=route['c'])
    if (not route['c'] or not all(task.get(k) for k in ('content_id', 'account_id', 'material_id'))
            or route['af_c_id'] == '0001' or route['content_id'] != expected['af_dp']
            or any(route[k] != v for k, v in expected.items() if k != 'af_dp')):
        return None
    try:
        url = urlsplit(route['long_url'])
        query = parse_qs(url.query, keep_blank_values=True, max_num_fields=30)
        if (url.scheme != 'https' or url.netloc != 'www.dramawavew2a.com'
                or url.path != '/ads/101/2250/view' or url.fragment
                or any(query.get(k) != [v] for k, v in expected.items())):
            return None
    except (ValueError, TypeError):
        return None
    return campaign_key(route['af_c_id'], route['c']), route['c']


def _legacy_sources(path):
    with snapshot(path) as db:
        routes = rows(db, """SELECT queue_id,code,af_channel,af_c_id,c,af_adset_id,af_ad_id,
            content_id,long_url,created_at FROM tt_post_code_route""")
        tasks = rows(db, """SELECT q.id,q.account_id,q.account_username,q.account_display_name,
            q.material_id,q.content_id,q.drama_name,q.material_language AS language,q.publish_id,
            q.code,p.created_by_user_id AS pool_creator,
            CASE WHEN s.id IS NOT NULL THEN r.created_by_user_id END AS recurring_creator,
            e.created_at AS confirmed_at
            FROM tt_post_queue q
            LEFT JOIN tt_post_material_pool p ON p.id=q.pool_item_id AND p.material_id=q.material_id
            LEFT JOIN tt_post_recurring_pool r ON r.queue_id=q.id AND r.material_id=q.material_id
                AND r.account_id=q.account_id AND r.content_id=q.content_id
            LEFT JOIN tt_post_schedule_run s ON s.id=r.run_id AND s.queue_id=q.id
                AND s.pool_item_id=r.id AND s.account_id=q.account_id
            LEFT JOIN tt_post_event e ON e.queue_id=q.id AND e.to_status='published'
                AND json_extract(e.details_json,'$.publish_id')=q.publish_id
            WHERE q.status='published' AND q.unknown_outcome=0 AND q.privacy_level='PUBLIC_TO_EVERYONE'""")
    return {r['queue_id']: r for r in routes}, tasks


def _auto_sources(path):
    with snapshot(path) as db:
        return rows(db, """SELECT t.id,t.account_id,t.account_username,t.account_display_name,
            t.material_id,t.content_id,t.drama_language AS language,t.publish_id,
            t.published_at_utc AS confirmed_at,t.template_id,
            COALESCE(NULLIF(json_extract(t.selection_json,'$.material.drama_name'),''),
                     json_extract(t.selection_json,'$.drama.name')) AS drama_name,
            CASE WHEN g.template_id=t.template_id AND g.template_version=t.template_version
                AND v.config_sha256=g.config_sha256 AND m.task_id=t.id
                AND parent.created_by_user_id=first.created_by_user_id AND parent.created_at=first.created_at
                THEN first.created_by_user_id END AS template_creator
            FROM tt_auto_task t
            LEFT JOIN tt_auto_run g ON t.run_id=g.id
            LEFT JOIN tt_auto_template parent ON t.template_id=parent.id
            LEFT JOIN tt_auto_template_version first ON first.template_id=t.template_id AND first.version=1
            LEFT JOIN tt_auto_template_version v ON v.template_id=t.template_id AND v.version=t.template_version
            LEFT JOIN tt_auto_material_ledger m ON m.material_id=t.material_id AND m.content_id=t.content_id
                AND m.task_id=t.id AND m.template_id=t.template_id AND m.run_id=t.run_id
            WHERE t.status='published' AND t.unknown_outcome=0
                AND json_extract(t.account_settings_json,'$.privacy_level')='PUBLIC_TO_EVERYONE'""")


def _previous(app):
    # A user directory is current state. Retain the tenant resolved at first
    # collection, even if that same user later changes tenants.
    root = Path(app.get('SOCIAL_ANALYTICS_WAREHOUSE_ROOT') or os.environ.get('SOCIAL_ANALYTICS_WAREHOUSE_ROOT')
                or '/mnt/data-disk/social-analytics-warehouse')
    manifest = root / 'current.json'
    if not manifest.exists():
        return {}
    path = (root / json.loads(manifest.read_text(encoding='utf-8'))['database']).resolve()
    if path.parent != (root / 'snapshots').resolve() or path.suffix != '.sqlite3':
        raise ReportError('TikTok 历史归属快照路径无效', 503)
    with snapshot(path) as db:
        prior = [json.loads(r[0]) for r in db.execute(
            "SELECT payload FROM catalog WHERE json_extract(payload,'$.platform')='tiktok'")]
    return {row['link_id']: row for row in prior if 'tt_identity' in row}


def _catalog_row(task, route, users, previous, kind):
    local_id = str(task['id'])
    route_missing = route is None
    if kind == 'post':
        creators = {x for x in (task['pool_creator'], task['recurring_creator']) if x}
        creator = next(iter(creators)) if len(creators) == 1 else ''
        if route and route['code'] != task['code']:
            route = None
    else:
        creator = task['template_creator'] or ''
    link = _route_key(route, task)
    confirmed_day = utc_day(task['confirmed_at'])
    if not confirmed_day or not task['publish_id']:
        raise ReportError('TikTok 公开发布记录缺少确认日期或平台回执', 503)
    # A successful receipt exists independently of the mutable short-code pool.
    # A publication-only key never collides with a real metric campaign_key.
    link_id = kind + ':' + local_id
    key = link or ('tt-publication:' + link_id, '')
    record = normalized('tiktok', dict(
        site_id='2250', key=list(key), link_id=link_id, tenant='', owner='', owner_label='待归属',
        drama=str(task['content_id'] or ''), drama_label=task['drama_name'] or task['content_id'] or '未记录',
        channel=str(task['account_id'] or ''),
        channel_label=task['account_display_name'] or task['account_username'] or str(task['account_id']),
        language=task['language'] or '未记录', language_label=task['language'] or '未记录',
        link_type='auto_template' if kind == 'auto' else 'tt_post',
        link_type_label='自动模板' if kind == 'auto' else 'TikTok Post',
        date=utc_day(route['created_at']) if link else '', excluded=False,
        publications=[dict(id='tiktok:post:' + task['publish_id'], date=confirmed_day, kind='video')]))
    # Source IDs are frozen separately from labels so a renamed account or
    # drama can never change its historical owner.
    evidence = [creator, str(task.get('template_id') or ''), record['channel'], record['drama'],
                str(task['material_id']), task['publish_id']]
    record['tt_identity'] = evidence
    prior = previous.get(record['link_id'])
    if prior:
        record['tt_identity'] = prior['tt_identity']
        if prior['tt_identity'] == evidence:
            record.update(prior['tt_frozen_owner'])
            # The publisher's short-code pool supports recycling. Keep the
            # verified historical campaign pair after its code is recycled.
            prior_route = prior.get('tt_frozen_route')
            if prior_route:
                record['tt_frozen_route'] = prior_route
                if route_missing or (link and list(link) == prior_route['key']):
                    record.update(prior_route)
                else:
                    record.update(key=['tt-publication:' + link_id, ''], date='')
        else:
            record.update(key=['tt-publication:' + link_id, ''], date='')
            if prior.get('tt_frozen_route'):
                record['tt_frozen_route'] = prior['tt_frozen_route']
    elif creator and len(users[creator]) == 1:
        person = users[creator][0]
        if person['tenant_key']:
            record.update(owner=creator, tenant=person['tenant_key'], owner_label=person['name'] or creator)
    record['tt_frozen_owner'] = (prior['tt_frozen_owner'] if prior else
                                 {field: record[field] for field in ('tenant', 'owner', 'owner_label')})
    if 'tt_frozen_route' not in record and link and record['key'] == list(link):
        record['tt_frozen_route'] = dict(key=record['key'], date=record['date'])
    record['platform_only'] = not bool(record['owner'] and record['tenant'])
    return record


def read_tiktok(app):
    """Return all confirmed public posts, including those without a personal owner."""
    routes, legacy = _legacy_sources(app.get('SOCIAL_ANALYTICS_TT_DB_PATH') or POST_DB)
    automatic = _auto_sources(app.get('SOCIAL_ANALYTICS_TT_AUTO_DB_PATH') or AUTO_DB)
    users = defaultdict(list)
    with snapshot(app.get('JOB_DB_PATH') or USERS_DB) as db:
        for person in rows(db, 'SELECT user_id,tenant_key,name FROM drama_admin_user'):
            users[person['user_id']].append(person)
    previous = _previous(app)
    # A second reservation of the same pair can exist in either namespace
    # even before it has a receipt. Such traffic has no unique source task.
    route_queues = defaultdict(set)
    for queue_id, route in routes.items():
        route_queues[campaign_key(route['af_c_id'], route['c'])].add(queue_id)
    catalog = {}
    for kind, tasks, offset in [('post', legacy, 0), ('auto', automatic, AUTO_QUEUE_ID_NAMESPACE)]:
        for task in tasks:
            queue_id = offset + task['id']
            row = _catalog_row(task, routes.get(queue_id), users, previous, kind)
            if route_queues.get(row['key'][0], set()) - {queue_id}:
                row.update(key=['tt-publication:%s:%s' % (kind, task['id']), ''], date='')
                if row['link_id'] not in previous:
                    row.pop('tt_frozen_route', None)
            existing = catalog.setdefault(row['link_id'], row)
            if existing != row:
                raise ReportError('同一 TikTok 任务有冲突的确认时间或归属证据', 503)
    return list(catalog.values())
