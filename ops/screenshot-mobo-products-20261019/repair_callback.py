"""Install the receipt fix and reconcile only this deployment's two rejected canaries.

Never uploads or regenerates. The original deployment backup remains the full rollback.
"""
import argparse
import ast
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import acceptance
import deploy

JOBS = {'984145dd754346369b1263afab73ee9b': '3360', 'bd8c36b18ce941e0b0c2760dfc3e1add': '2373'}
FUNCTION = 'post_screenshot_ai_source_callback'
EXPECTED_APP = 'f31d2c04350507f6bfede00ca4c6add1a0e6dbe92ac011e0bbc055a1d3c943ce'


def prepare():
    target = deploy.LIVE / 'app.py'
    current = target.read_text()
    assert deploy.sha(target.read_bytes()) == EXPECTED_APP, 'live application drift'
    source = (deploy.ROOT / 'app.py').read_text()
    old = deploy.function_nodes(current)
    new = deploy.function_nodes(source)
    lines = current.splitlines(keepends=True)
    node = old[FUNCTION]
    lines[node.lineno - 1:node.end_lineno] = [ast.get_source_segment(source, new[FUNCTION]) + '\n']
    candidate = ''.join(lines)
    result = deploy.function_nodes(candidate)
    for name, value in old.items():
        if name != FUNCTION:
            assert deploy.ast_sha(value) == deploy.ast_sha(result[name]), 'unrelated function changed: ' + name
    compile(candidate, '<receipt-fix>', 'exec')
    jobs = []
    with closing(acceptance.db()) as conn:
        for job_id, app_id in JOBS.items():
            job = dict(conn.execute('SELECT * FROM drama_screenshot_job WHERE job_id=?', (job_id,)).fetchone())
            assert job['app_id'] == app_id and job['content_id'] == '67767322' and job['status'] == 'done'
            row = conn.execute("SELECT detail_json FROM drama_admin_audit_log WHERE target_id=? AND action='callback_screenshot_ai_source' ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
            receipt = json.loads(row[0])
            body = json.loads(receipt['response'])
            assert body['code'] == -1 and 'app_package' in body['msg'], 'known product rejection required'
            jobs.append({'job': job, 'receipt': receipt})
    rows = acceptance.material_rows(['67767322'])
    names = {f"67767322-{item['job']['drama_name']}-{ratio}-{item['job']['job_id'][:8]}"
             for item in jobs for ratio in acceptance.RATIOS.values()}
    assert not any(row['name'] in names for row in rows), 'existing material requires reconciliation before mutation'
    return candidate.encode(), jobs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    assert not deploy.command('git', '-C', str(deploy.ROOT), 'status', '--porcelain')
    commit = deploy.command('git', '-C', str(deploy.ROOT), 'rev-parse', 'HEAD')
    candidate, jobs = prepare()
    print(json.dumps({'mode': 'apply' if args.apply else 'preflight', 'commit': commit,
                      'app_sha256': deploy.sha(candidate), 'reclassify_rejected_canaries': list(JOBS)}))
    if not args.apply:
        return
    with open(str(deploy.LIVE / 'logs/auto_cover_synthesis.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deploy.assert_drained()
        state_path = deploy.STATE / 'deployment.json'
        state = json.loads(state_path.read_text())
        assert state['status'] == 'deployed' and not state.get('cron_products_activated')
        backup = deploy.STATE / 'before-receipt-fix'
        backup.mkdir()
        target = deploy.LIVE / 'app.py'
        shutil.copy2(str(target), str(backup / 'app.py'))
        shutil.copy2(str(state_path), str(backup / 'deployment.json'))
        (backup / 'rejected-canaries.json').write_text(json.dumps(jobs, ensure_ascii=False, indent=2))
        with closing(deploy.read_db()) as src:
            with sqlite3.connect(str(backup / 'jobs.sqlite3')) as dst:
                src.backup(dst)
        record = next(r for r in state['files'] if r['path'] == str(target))
        record['expected_sha256'] = deploy.sha(candidate)
        state['pending_receipt_fix'] = {'commit': commit, 'backup': str(backup)}
        # Keep complete rollback possible even if interrupted between file swap
        # and final state bookkeeping.
        state_path.write_text(json.dumps(state, indent=2))
        pids = deploy.worker_pids()
        subprocess.check_call(['systemctl', 'stop', 'drama-material-api'])
        try:
            temporary = target.with_name('app.py.receipt-new')
            temporary.write_bytes(candidate)
            os.chmod(str(temporary), target.stat().st_mode & 0o777)
            os.replace(str(temporary), str(target))
            subprocess.check_call(['systemctl', 'start', 'drama-material-api'])
            deploy.verify_api()
            assert pids == deploy.worker_pids(), 'generation workers must not restart'
        except Exception:
            shutil.copy2(str(backup / 'app.py'), str(target))
            subprocess.check_call(['systemctl', 'restart', 'drama-material-api'])
            raise
        # A definitive business rejection and empty material readback allow these
        # new acceptance records to be corrected, without an outbound retry.
        with sqlite3.connect(str(deploy.LIVE / 'data/drama_material_jobs.sqlite3'), timeout=10) as conn:
            conn.execute('BEGIN IMMEDIATE')
            for item in jobs:
                job = item['job']
                current = conn.execute('SELECT status,square_1x1_url,landscape_1_91x1_url,portrait_4x5_url FROM drama_screenshot_job WHERE job_id=?', (job['job_id'],)).fetchone()
                assert current == ('done', *(job[field] for field in acceptance.SIZES)), 'canary changed since readback'
                conn.execute("UPDATE drama_screenshot_job SET status='failed', progress=96, progress_detail=?, error_message=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND status='done'",
                             ('三种封面已生成；素材后台拒绝产品导入，等待接口支持',
                              '素材 app_package 不支持导入；HTTP 200/code=-1，未入库。已保留图片，请勿重新生成。', job['job_id']))
                conn.execute('INSERT INTO drama_admin_audit_log(action,target_type,target_id,detail_json) VALUES (?,?,?,?)',
                             ('reconcile_screenshot_callback_rejection', 'screenshot_job', job['job_id'],
                              json.dumps({'from': 'done', 'to': 'failed', 'reason': 'known_business_rejection',
                                          'original_receipt': item['receipt'], 'generated_assets_preserved': True,
                                          'outbound_requests': 0}, ensure_ascii=False)))
        record['after_sha256'] = record['expected_sha256'] = deploy.sha(candidate)
        state.setdefault('patches', []).append({'previous_commit': state['commit'], 'commit': commit,
                                                'backup': str(backup), 'purpose': 'validate upload business receipt'})
        state['commit'] = commit
        state.pop('pending_receipt_fix', None)
        state['upload_acceptance'] = 'blocked_by_upstream_product_allowlist'
        state_path.write_text(json.dumps(state, indent=2))
        (deploy.STATE / 'receipt-fix.json').write_text(json.dumps({'commit': commit, 'jobs': list(JOBS),
                         'status': 'failed', 'generated_assets_preserved': True, 'outbound_requests': 0,
                         'app_sha256': record['after_sha256']}, indent=2))
        print('FIXED: strict business receipts; two rejected canaries corrected; six images retained; cron unchanged')


if __name__ == '__main__':
    main()
