"""Reviewed one-time Page configuration import through the existing service."""
import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import urllib.request
from .runtime import shared,encoded,atomic_json
shared()
from features.fb_video_publish.client import request
from features.social_analytics.warehouse_refresh import environment,Reader


def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',required=True);p.add_argument('--receipt',required=True)
    p.add_argument('--apply',action='store_true');p.add_argument('--expected-sha256');a=p.parse_args()
    source=Path(a.manifest).read_bytes();m=json.loads(source)
    digest=hashlib.sha256(source).hexdigest()
    assert m['group_id']==62 and len(m['members'])==236 and len({r['page_id'] for r in m['members']})==236
    assert m['tenant_key']=='149bb4a14b57975f' and m['user_id']=='892fd2e8'
    assert sum(not r['language'] for r in m['members'])==20
    env=environment('/root/drama_material_service/.env')
    # Current identity is resolved by the existing trusted broker, including
    # active membership, module permission, navigation and business mapping.
    req=urllib.request.Request('http://127.0.0.1:8787/internal/fb-video-publish/identity',
          data=encoded(dict(tenant_key=m['tenant_key'],user_id=m['user_id'])).encode(),
          headers={'Content-Type':'application/json','Authorization':'Bearer '+env['FB_AUTO_POST_INTERNAL_TOKEN']},method='POST')
    with urllib.request.urlopen(req,timeout=30) as response:identity=json.load(response)
    actor=identity['actor']
    assert actor['user_name']=='郜远' and actor['is_admin']
    current=Reader(env).query(["SELECT HEX(JSON_OBJECT('page_id',page_id,'language',language)) FROM kunlunads_dev.ads_facebook_page_group_ins WHERE group_id=62 AND deleted_at=0"],timeout=30)
    assert sorted(current,key=lambda r:r['page_id'])==sorted(m['members'],key=lambda r:r['page_id']),'group_drift'
    before=request('GET','/api/fb-video-publish/pages',actor=actor,environ=env)['items']
    existing={r['page_id']:r for r in before}
    overlap=[r for r in m['members'] if r['page_id'] in existing]
    if overlap:
        # A completed or partially completed invocation can be reconciled.
        assert all(existing[r['page_id']]['created_by']==actor['user_id'] and existing[r['page_id']]['language']==r['language'] for r in overlap),'existing_configuration_requires_review'
    additions=[r for r in m['members'] if r['page_id'] not in existing]
    report=dict(manifest_sha256=digest,group_id=62,creator=actor['user_name'],creator_id=actor['user_id'],before=len(before),new=len(additions),already_imported=len(overlap),languages=dict(Counter(r['language'] for r in additions)))
    if not a.apply:
        atomic_json(a.receipt,report);print(encoded(report));return
    assert a.expected_sha256==digest,'apply_requires_reviewed_manifest_hash'
    backup=Path(a.receipt).with_suffix('.before.sqlite3')
    assert not backup.exists(),'backup_already_exists_use_original_receipt_for_reconciliation'
    with sqlite3.connect('file:/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3?mode=ro',uri=True) as source_db,sqlite3.connect(backup) as target:
        source_db.backup(target)
    atomic_json(Path(a.receipt).with_suffix('.before.json'),before)
    by_language=defaultdict(list)
    for row in additions:by_language[row['language']].append(row['page_id'])
    for lang,ids in sorted(by_language.items()):
        for start in range(0,len(ids),200):
            request('POST','/api/fb-video-publish/pages',actor=actor,environ=env,
                    payload=dict(page_ids=ids[start:start+200],language=lang,caption_template='',title=''))
    after=request('GET','/api/fb-video-publish/pages',actor=actor,environ=env)['items']
    result={r['page_id']:r for r in after}
    assert all(result[r['page_id']]['created_by']==actor['user_id'] and result[r['page_id']]['created_by_name']==actor['user_name'] and result[r['page_id']]['language']==r['language'] for r in m['members'])
    assert all(result[r['page_id']]==r for r in before if r['page_id'] not in {x['page_id'] for x in m['members']}),'unrelated_configuration_changed'
    report.update(after=len(after),verified_group_pages=236,blank_language=20,backup=str(backup),status='applied')
    atomic_json(a.receipt,report);print(encoded(report))


if __name__=='__main__':main()
