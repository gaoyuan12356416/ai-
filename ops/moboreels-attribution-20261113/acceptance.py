"""Production acceptance without creating social posts or a second upstream promotion."""
import argparse
import datetime
import json
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start-report', 'report-status', 'short-link', 'report-query'])
    parser.add_argument('--run-id', default='')
    args = parser.parse_args()
    sys.path.insert(0, '/opt/moboreels-library/current')
    from features.moboreels_library.settings import Settings
    from features.moboreels_library.db import Database, _literal
    from features.moboreels_library.actors import resolve_actor
    settings = Settings.from_env()
    db = Database(settings)
    with sqlite3.connect(str(settings.data_dir/'promotions.sqlite3')) as conn:
        promotions = [json.loads(row[0]) for row in conn.execute('SELECT record_json FROM promotions')]
    record = next(row for row in promotions if row.get('status') == 'succeeded'
                  and row.get('story_id') == '194369322' and row.get('koc_code') == '322CLQ')
    actor = resolve_actor(settings, record['user_id'], record['tenant_key'])

    def request(path, body, macro=False):
        req = urllib.request.Request('http://127.0.0.1:8864'+path,
            data=json.dumps(body).encode(), headers={'Content-Type':'application/json',
            'Authorization':'Bearer '+(settings.macro_token if macro else settings.internal_token)})
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.load(response)

    def dispatch(method, path, body=None, query=None, admin=False):
        return request('/internal/dispatch', {'method':method, 'path':path,
            'actor':dict(actor, is_admin=admin), 'body':body or {}, 'query':query or {}})

    end = datetime.datetime.now(datetime.timezone.utc).date()-datetime.timedelta(days=1)
    period = {'date_from':str(end-datetime.timedelta(days=30)), 'date_to':str(end)}
    if args.action == 'start-report':
        result = dispatch('POST','/report-sync-runs',period)
        print(json.dumps({'run':result['run'], 'reused':result['reused']}, ensure_ascii=False))
    elif args.action == 'report-status':
        result = dispatch('GET','/report-sync-runs/'+(args.run_id or 'latest'))
        print(json.dumps(result, ensure_ascii=False))
    elif args.action == 'report-query':
        institution = dispatch('GET','/reports', query=period, admin=True)
        owner = dispatch('GET','/reports', query=period)
        print(json.dumps({'institution':institution, 'owner':owner}, ensure_ascii=False))
    else:
        body = {'actor':actor, 'story_id':record['story_id'], 'platform':'youtube',
            'account_id':'moboreels-validation', 'task_id':'moboreels-attribution-20261113-acceptance',
            'material_id':'validation-story-194369322', 'purpose':'validation'}
        one = request('/internal/short-links', body, macro=True)
        two = request('/internal/short-links', body, macro=True)
        assert one == two and one['status'] == 'succeeded'
        assert one['koc_code'] == record['koc_code']
        assert one['promotion_request_id'] == record['request_id']
        with sqlite3.connect(str(settings.data_dir/'promotions.sqlite3')) as conn:
            observed = [json.loads(row[0]) for row in conn.execute('SELECT record_json FROM promotions')]
        selected = [row for row in observed if row.get('star_user_id')==record['star_user_id']
                    and row.get('story_id')==record['story_id'] and row.get('institution_user_id')==record['institution_user_id']]
        assert len(selected)==1 and selected[0]['request_id']==record['request_id']
        assert selected[0]['koc_code']==record['koc_code'] and selected[0]['deep_link']==record['deep_link']
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        try:
            response = opener.open(one['short_url'], timeout=25)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            assert response.code == 302
            assert response.headers['Location'] == record['deep_link']
        sql = "SELECT JSON_OBJECT('short_link_id',short_link_id,'promotion_key',promotion_key,'status',status,'purpose',purpose) FROM ads_ai.cdreader_koc_short_link WHERE short_link_id="+_literal(one['short_link_id'])+';'
        rows = []
        for _ in range(6):
            rows = db._read(sql)
            if rows:
                break
            time.sleep(5)
        assert rows and rows[0]['status']=='succeeded' and rows[0]['purpose']=='validation'
        print(json.dumps({'reused':True, 'unchanged_selected_promotion':True,
            'redirect_status':302, 'mysql_mirrored':True, 'short_link':one}, ensure_ascii=False))


if __name__ == '__main__':
    main()
