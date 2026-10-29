"""GET-only final account-library audit; never edits original DELETE receipts."""
import argparse
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from finish_meta_creative_scope_once import read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--after-controller', action='store_true')
    args = parser.parse_args()
    assert Path('/mnt/data-disk').resolve() in args.output.resolve().parents
    if args.after_controller:
        while True:
            path = args.output.parent/'continuation-controller.json'
            if path.exists() and json.loads(path.read_text()).get('stage') == 'finished':
                break
            state = subprocess.check_output(['systemctl','show','meta-scope-continuation-20261029.service','-p','ActiveState','--value'], text=True).strip()
            assert state in ('activating', 'active'), 'Continuation did not finish; no audit started'
            time.sleep(30)
    job = read('SELECT * FROM fb_asset_delete_v2_jobs WHERE job_id=?', (args.job,))[0]
    metadata = json.loads(job['metadata'])
    assert job['status'] != 'running'
    assert set(metadata['ids']) == {'XEY147', 'XEY50328'}
    assert {str(p['id']) for p in metadata['products']} == {'2373', '3360', '2472', '3443'}
    live = Path('/root/drama_material_service')
    pid = subprocess.check_output(['systemctl','show','drama-material-api.service','-p','MainPID','--value'], text=True).strip()
    for item in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        if b'=' in item:
            key, value = item.split(b'=', 1)
            os.environ[key.decode()] = value.decode()
    os.chdir(live)
    sys.path.insert(0, str(live))
    import app
    from features.fb_ad_asset_delete.source import SqlSource
    from features.fb_ad_asset_delete.graph import GraphClient
    from features.fb_ad_asset_delete.core import AssetError
    from features.fb_ad_asset_delete.execution import SOURCE_READ_SLOTS

    def query(sql, timeout):
        with SOURCE_READ_SLOTS:
            return app.ad_control_run_mysql(sql, timeout_seconds=timeout, via_stdin=True)
    assert str(app.MYSQL_HOST) == '101.32.56.53' and str(app.MYSQL_PORT) == '63350'
    assert query('SELECT @@read_only', 10) == [['1']]
    source = SqlSource(query, schema=app.AD_CONTROL_DB_NAME)
    groups, outcomes = defaultdict(list), []
    children = read('SELECT object_key,account_id,status,result FROM fb_asset_delete_v2_video_accounts WHERE job_id=?', (args.job,))
    for row in children:
        result = json.loads(row['result'])
        vid = row['object_key'].split(':', 1)[1]
        if row['status'] in ('deleted', 'already_deleted'):
            outcomes.append(dict(video_id=vid, account_id=row['account_id'], status='deletion_receipt'))
        else:
            groups[(row['account_id'], str(result.get('credential_user_id', '')))].append(vid)
    audits = []

    def save(stage):
        final = dict(job_id=args.job, stage=stage, delete_requests=0, ledger_writes=0,
            checked_at=int(time.time()), account_groups=len(groups), checked_groups=len(audits),
            pair_counts=dict(Counter(row['status'] for row in outcomes)), audits=audits, pairs=outcomes)
        tmp = args.output.with_suffix('.tmp')
        tmp.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(args.output)
        print(json.dumps({k: final[k] for k in ('stage', 'checked_groups', 'account_groups', 'pair_counts')}, ensure_ascii=False), flush=True)

    def inspect(group, videos):
        aid, uid = group
        audit = dict(account_id=aid, credential_user_id=uid, started_at=int(time.time()))
        client = GraphClient(source.token)
        try:
            token = source.token([uid])
            if not token:
                raise AssetError('credential_unavailable', '原删除用户的当前凭证不可用，无法完成账户读取')
            library = client._read_video_account_inventory(aid, token, seconds=60)
            assert library['complete']
            audit.update(complete=True, pages=library['pages'], items=len(library['ids']),
                inventory_sha256=hashlib.sha256('\n'.join(sorted(library['ids'])).encode()).hexdigest())
            rows = [dict(video_id=vid, account_id=aid, status='still_present' if vid in library['ids'] else 'confirmed_absent') for vid in videos]
        except AssetError as exc:
            audit.update(complete=False, code=exc.code, message=exc.message)
            rows = [dict(video_id=vid, account_id=aid, status='unverified') for vid in videos]
        finally:
            client.http.close()
        audit['finished_at'] = int(time.time())
        return audit, rows

    save('reading')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(inspect, group, videos) for group, videos in groups.items()]
        for future in as_completed(futures):
            audit, rows = future.result()
            audits.append(audit)
            outcomes.extend(rows)
            save('reading')
    save('finished')


if __name__ == '__main__':
    main()
