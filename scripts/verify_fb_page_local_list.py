"""Read-only production checks with the two supplied trusted ordinary actors.

Bearer is read into memory from the running FB process. Only /pages GETs are
issued; this script cannot create configurations, batches or Facebook posts.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


def main(output):
    root = Path('/opt/fb-auto-post/current').resolve()
    sys.path.insert(0, str(root))
    from features.fb_video_publish.client import request, GatewayError, query_params
    pid = subprocess.check_output(['systemctl', 'show', 'fb-auto-post-service.service', '-p', 'MainPID', '--value'], text=True).strip()
    env = dict(v.split('=', 1) for v in Path('/proc', pid, 'environ').read_text().split('\0') if '=' in v)
    actors = [dict(tenant_key='149bb4a14b57975f', user_id=uid, user_name=name, is_admin=False,
                   business_owner_id=owner, sub_user_id=owner) for uid, name, owner in
              [('cbb48d2b', '邱心敏', '947'), ('d62868g5', '朱锦茵', '904')]]
    evidence = dict(at_utc=datetime.now(timezone.utc).isoformat(), runtime=str(root), fb_pid=pid, checks=[], read_only=True)
    db = Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
    with sqlite3.connect(db.as_uri()+'?mode=ro', uri=True) as conn:
        expected = conn.execute('SELECT count(*) FROM fb_video_page WHERE tenant_key=? AND deleted=0', (actors[0]['tenant_key'],)).fetchone()[0]
    for actor in actors:
        started = time.monotonic()
        data = request('GET', '/api/fb-video-publish/pages', actor=actor, environ=env)
        elapsed = round(time.monotonic()-started, 3)
        assert len(data['items']) == expected and expected >= 3900, 'local_catalog_incomplete'
        assert all(p['authorization_unknown'] and p['eligible_token_count'] == 0 and not p['blocked_reason'] for p in data['items']), 'unknown_marked_authorized'
        assert elapsed < 5, 'local_list_too_slow'
        record = dict(user=actor['user_name'], is_admin=False, count=expected, list_seconds=elapsed, unknown_explicit=True, batches=[])
        evidence['checks'].append(record)
        # One complete display page for Qiu, one batch for Zhu; each sequential
        # call goes through the unchanged broker / FIFO / replica checks.
        ids = [p['page_id'] for p in data['items'][:20 if actor['sub_user_id'] == '947' else 8]]
        for offset in range(0, len(ids), 8):
            batch = ids[offset:offset+8]
            query = query_params('verify_ids='+','.join(batch))
            started = time.monotonic()
            try:
                verified = request('GET', '/api/fb-video-publish/pages', actor=actor, query=query, environ=env)['items']
                assert [p['page_id'] for p in verified] == batch, 'verification_dropped_config'
                assert all(not p['authorization_unknown'] and (p['eligible_token_count'] > 0 or p['blocked_reason']) for p in verified), 'authorization_not_explicit'
                assert all('version' not in p and 'caption_template' not in p for p in verified), 'authorization_contains_editable_config'
                record['batches'].append(dict(size=len(batch), status='verified', available=sum(p['eligible_token_count']>0 and not p['blocked_reason'] for p in verified), seconds=round(time.monotonic()-started, 3)))
            except GatewayError as exc:
                assert exc.code in {'source_busy', 'source_query_failed', 'source_query_timeout', 'source_connection_failed'}, 'unexpected_verification_failure'
                record['batches'].append(dict(size=len(batch), status=exc.code, local_failure=True))
            output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
        # Invalid size and out-of-config requests fail before a source read.
        for query, status in [(dict(verify_ids=','.join(p['page_id'] for p in data['items'][:9])), 400), (dict(verify_ids='9'*31), 404)]:
            try:
                request('GET', '/api/fb-video-publish/pages', actor=actor, query=query, environ=env)
                raise AssertionError('invalid_verification_accepted')
            except GatewayError as exc:
                assert exc.status == status, 'wrong_validation_status'
        record['invalid_scope_and_size_rejected'] = True
        refreshed = request('GET', '/api/fb-video-publish/pages', actor=actor, environ=env)
        assert len(refreshed['items']) == expected, 'refresh_lost_list'
    assert any(b['status'] == 'verified' for c in evidence['checks'] for b in c['batches']), 'no_live_verification_succeeded'
    evidence['ok'] = True
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert args.output.resolve().is_relative_to('/mnt/data-disk')
    main(args.output)
