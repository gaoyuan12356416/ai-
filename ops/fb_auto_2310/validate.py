"""Exercise installed source variants without publishing or sending messages."""
import argparse
import contextlib
import html
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlsplit

OLD = 'https://www.dramawavew2a.com/ads/0/2049/view'
NEW = 'https://www.dramawavew2a.com/ads/101/2310/view'


def check_runtime(root, mode):
    sys.path.insert(0, str(root))
    if mode == 'fb':
        from features.fb_auto_posts.links import build_w2a_url, validate_w2a_url
        url = build_w2a_url(dict(username='10001', timestamp=1787191200,
            material_language='en', drama_name='A Drama', tag='hook', task_id=7,
            page_name='Page', page_id='10001', material_name='Video', material_id='501', content_id='AcWE9aQz8q'))
        assert url.startswith(NEW + '?')
        assert validate_w2a_url(url.replace(NEW, OLD, 1))
        print(json.dumps(dict(mode=mode, new_base=NEW, legacy_frozen_compatible=True)))
        return
    from features.social_analytics.facebook import parse_link
    from features.social_analytics.report import campaign_index, effect_sites, parameters, summarize
    from features.social_analytics.backend_scope import restrict
    from features.social_analytics.adapters import normalized
    day = '2026-10-25'
    row = normalized('facebook', dict(key=('7', 'frozen campaign'), link_id='facebook:2049:7',
        site_id='2049', owner='owner', owner_label='owner', tenant='tenant', channel='facebook:page',
        drama='drama', drama_label='Drama', language='en', date=day, excluded=False,
        link_type='auto_template', publications=[dict(id='facebook:page:1', date=day, kind='post')]))
    row['effect_source'] = dict(adset_id='page', ad_id='material')
    assert effect_sites(row) == ('2049', '2310')
    assert campaign_index([row])[('facebook', '2310', '7')][0]['owner'] == 'owner'
    values = [dict(platform='facebook', site_id=site, date=day, campaign_id='7', campaign='frozen',
        adset_id='page', ad_id='material', revenue_cents=amount, refund_cents=0,
        installs=1, clicks=2, views=3, conversions=1) for site, amount in [('2049', 100), ('2310', 200)]]
    catalog, accepted, coverage, _ = restrict([row], values, day)
    report = summarize(catalog, accepted, {'_system_report': True}, parameters(dict(
        start=[day], end=[day], platform=['facebook'], group_by=['owner,platform'])),
        'installed-check', metric_coverage=coverage)
    assert report['totals']['revenue_cents'] == 300
    assert report['totals']['published_count'] == 1
    assert report['totals']['links'] == 1
    bad = dict(values[1], ad_id='foreign')
    assert restrict([row], [bad], day)[1] == []
    with contextlib.closing(sqlite3.connect('file:/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3?mode=ro', uri=True)) as conn:
        sample = conn.execute("SELECT id,long_url FROM fb_auto_task WHERE long_url LIKE ? LIMIT 1", (OLD+'?%',)).fetchone()
    assert sample
    parse_link(sample[1], '2049', str(sample[0]))
    parse_link(sample[1].replace(OLD, NEW, 1), '2049', str(sample[0]))
    print(json.dumps(dict(mode=mode, root=str(root), both_sites_attributed=True,
                          published_count=1, revenue_cents=300, foreign_material_rejected=True)))


def verify_links(folder):
    import requests
    db = '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'
    root = Path('/mnt/data-disk/fb-auto-post-public/s2l/fb')
    with contextlib.closing(sqlite3.connect('file:' + db + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('ATTACH DATABASE ? AS before', ('file:' + str(folder / 'fb-before.sqlite3') + '?mode=ro',))
        changed = conn.execute("""SELECT COUNT(*) FROM before.fb_auto_task b LEFT JOIN fb_auto_task t ON t.id=b.id
            WHERE b.status='published' AND b.long_url LIKE ? AND (t.id IS NULL OR t.long_url<>b.long_url
            OR t.short_url<>b.short_url OR t.graph_post_id<>b.graph_post_id)""", (OLD+'?%',)).fetchone()[0]
        assert changed == 0, 'historical_frozen_links_or_receipts_changed'
        rows = conn.execute("SELECT id,long_url,short_url FROM fb_auto_task WHERE status='published' "
                            "AND long_url LIKE ? ORDER BY id", (OLD+'?%',)).fetchall()
        new = conn.execute("SELECT id,status,long_url,short_url FROM fb_auto_task WHERE long_url LIKE ? ORDER BY id DESC LIMIT 5", (NEW+'?%',)).fetchall()
    errors = []
    for task_id, original, short in rows:
        path = root / (str(task_id) + '.html')
        try:
            raw = path.read_bytes()
            served = raw.replace((OLD+'?').encode(), (NEW+'?').encode()).decode()
            targets = [html.unescape(value).removeprefix('0;url=') for value in
                       re.findall(r'(?:href|content)="([^\"]+)"', served) if NEW in value]
            targets.append(json.loads(re.search(r'location.replace\((.*?)\);', served).group(1)))
            assert len(targets) == 4
            assert all(url == original.replace(OLD, NEW, 1) for url in targets)
        except Exception as exc:
            errors.append(dict(task_id=task_id, error=type(exc).__name__))
    assert not errors, json.dumps(errors[:20])
    positions = sorted({round((len(rows)-1)*n/11) for n in range(12)})
    samples = [rows[i] for i in positions]
    samples.extend((r[0], r[2], r[3]) for r in new[:2])
    session = requests.Session()
    session.trust_env = False
    receipts = []
    for task_id, original, short in samples:
        response = session.get(short, timeout=(5, 20), allow_redirects=False)
        assert response.status_code == 200, 'short_http_status:' + str(task_id)
        expected = (root / (str(task_id)+'.html')).read_bytes().replace((OLD+'?').encode(), (NEW+'?').encode())
        assert response.content == expected, 'short_public_content:' + str(task_id)
        assert 'no-store' in response.headers.get('Cache-Control', '')
        receipts.append(dict(task_id=task_id, short_url=short, status=response.status_code, all_targets_2310=True))
    landing = []
    for task_id, original, short in samples[-3:]:
        target = original.replace(OLD, NEW, 1)
        response = session.get(target, timeout=(5, 25), allow_redirects=False)
        assert response.status_code == 200, 'landing_http_status:' + str(task_id)
        assert len(response.content) > 1000
        landing.append(dict(task_id=task_id, status=response.status_code, bytes=len(response.content)))
    result = dict(published_legacy_links_checked=len(rows), historical_links_receipts_unchanged=True,
                  redirect_parameter_errors=errors, public_short_checks=receipts,
                  landing_checks=landing, new_2310_tasks=[dict(id=r[0], status=r[1]) for r in new])
    (folder/'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    (folder/'verification.json').chmod(0o600)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--mode', choices=['fb', 'report'])
    parser.add_argument('--verify-links', type=Path)
    args = parser.parse_args()
    if args.verify_links:
        verify_links(args.verify_links)
    else:
        check_runtime(args.root, args.mode)
