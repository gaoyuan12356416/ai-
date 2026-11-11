"""Read-only exact-resource/channel matching audit. Never creates a run or refreshes tokens."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace


def environment(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            result[key] = value.strip().strip('"').strip("'")
    return result


def inspect(args):
    sys.path.insert(0, str(Path(args.runtime).resolve()))
    import pymysql
    from features.x_tag_publish.hit_drama import resolve, match_reason
    from features.hit_drama_target import content_ids
    from features.x_tag_publish.policy import Decision, portrait_snapshot, catalog_labels, language
    from features.x_auto_posts.x_sidecar import XPostAutoBridgeClient
    from features.x_auto_posts.service import XAutoPostService
    settings = environment('/etc/x-tag-publisher.env')
    os.environ.update(settings)
    path = Path(settings['X_AUTO_POST_DB_PATH'])
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = db.execute('SELECT t.name,t.current_version,v.config_json FROM x_auto_template t JOIN x_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.id=?', (args.template,)).fetchone()
        if row is None:
            raise ValueError('template_not_found')
        config = json.loads(row[2])
    finally:
        db.close()
    env = environment('/root/drama_material_service/.env')
    assert env['ADMIN_MAPPING_MYSQL_HOST'] == '101.32.56.53' and env['ADMIN_MAPPING_MYSQL_PORT'] == '63350'
    conn = pymysql.connect(host=env['ADMIN_MAPPING_MYSQL_HOST'], port=63350,
        user=env['ADMIN_MAPPING_MYSQL_USER'], password=env['ADMIN_MAPPING_MYSQL_PASSWORD'],
        database=env['ADMIN_MAPPING_MYSQL_DATABASE'], connect_timeout=5, read_timeout=20,
        write_timeout=5, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT @@read_only AS ro')
            assert cur.fetchone()['ro'] == 1
            cur.execute('SET SESSION MAX_EXECUTION_TIME=15000')
            cur.execute('START TRANSACTION READ ONLY')
            def query(sql, params):
                cur.execute(sql, params)
                return cur.fetchall()
            source = SimpleNamespace(schema=env['ADMIN_MAPPING_MYSQL_DATABASE'], app_id=1479, _stream_select=query)
            target = resolve(source, dict(id_type='resource_id', drama_id=args.resource))
            catalog = catalog_labels(source, [SimpleNamespace(content_id=cid) for cid in content_ids(target)])
    finally:
        conn.rollback()
        conn.close()
    bridge = XPostAutoBridgeClient(settings['X_POST_AUTOMATION_INTERNAL_URL'], settings['X_POST_AUTO_INTERNAL_TOKEN'])
    accounts = {str(a['id']): a for a in bridge.accounts()}
    chosen = [accounts[str(a)] for a in config['account_ids'] if str(a) in accounts]
    snapshot = portrait_snapshot([a.get('x_user_id') for a in chosen if a.get('publish_eligible')])
    results = []
    for account_id in config['account_ids']:
        account = accounts.get(str(account_id), {})
        lang = XAutoPostService._account_language(account)
        ids = content_ids(target, lang)
        decision = Decision(account, ['read-only-audit', args.resource], snapshot)
        pairs = [dict(content_id=cid, reason=match_reason(decision, catalog.get((cid, language(lang))))) for cid in ids]
        eligible = [p['content_id'] for p in pairs if not p['reason']]
        reason = ('account_not_publishable' if not account.get('publish_eligible') else
                  'language_not_matched' if not ids else '' if eligible else pairs[0]['reason'])
        results.append(dict(account_id=str(account_id), username=account.get('username'), language=lang,
            reason=reason, language_matched=bool(ids), matching_content_ids=eligible if not reason else [], dramas=pairs))
    proof = dict(resource_id=args.resource, template_id=args.template, template_name=row[0], template_version=row[1],
        target=target, catalog=list(catalog.values()), profile_meta=snapshot[1], accounts=results,
        account_count=len(results), matched_channels=sum(not r['reason'] for r in results),
        reason_counts=dict(Counter(r['reason'] or 'matched' for r in results)),
        matched_languages=dict(Counter(r['language'] for r in results if not r['reason'])),
        portuguese=dict(content_ids_pt=content_ids(target, 'pt'), content_ids_pt_br=content_ids(target, 'pt-br'),
            channels=sum(language(r['language']) == 'pt-br' for r in results),
            language_matched=sum(language(r['language']) == 'pt-br' and r['language_matched'] for r in results)),
        writes=0)
    if args.output:
        output = Path(args.output)
        output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
        output.chmod(0o600)
    print(json.dumps({k: v for k, v in proof.items() if k not in ('target', 'catalog', 'accounts')}, ensure_ascii=False))
    return proof


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--resource', required=True)
    parser.add_argument('--template', type=int, required=True)
    parser.add_argument('--runtime', default='/opt/x-tag-publisher/current')
    parser.add_argument('--output')
    inspect(parser.parse_args())
