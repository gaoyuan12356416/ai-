"""Restore only the verified November 23 five-episode selection, without rendering."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

SID='ss_d5101e79e7d8407d8d95765ca267c3ea'
ASSETS=['aa_b3419840640d4c56880675a5b7f4b5a4','aa_2b37854e8fef463fa28a7045c978c813',
        'aa_457d6f4518ab44448e9e1a511b7959b2','aa_0ebdcdfa24004fa2bd45d7d418efff1f','aa_7391bf54ca5e42159e16fe55c77ba006']


def main():
    sys.path.insert(0,'/opt/ai-video-editing/current')
    from ai_video_editing.runtime import Runtime
    from ai_video_editing import studio,selections
    rt=Runtime('/mnt/data-disk/ai-video-editing');studio.setup(rt)
    with rt.db() as db:
        s=db.execute('SELECT * FROM studio_sessions WHERE id=?',(SID,)).fetchone()
        rows=[db.execute('SELECT a.owner,a.episode,a.sha256,c.product,c.source_key FROM assets a JOIN catalog_imports c ON c.asset_id=a.id WHERE a.id=? AND c.status=\'ready\'',(aid,)).fetchone() for aid in ASSETS]
        assert s and all(rows)
        assert all(r['owner']==s['owner'] and r['episode']==i+1 and r['source_key']==rows[0]['source_key'] and r['product']==rows[0]['product'] for i,r in enumerate(rows))
        runs=db.execute('SELECT count(*) FROM studio_runs WHERE session=?',(SID,)).fetchone()[0]
    tenant,user=json.loads(s['owner']);actor={'tenant_key':tenant,'user_id':user}
    op='restore-five-20261123-'+SID
    prior=selections.operation(rt,actor,op)
    if prior:print(json.dumps({'restored':True,'reused_receipt':True,**prior}));return
    if s['version']!=2 or s['updated']!=1795418888.6562696 or json.loads(s['sources'])!=ASSETS[:1]:
        print(json.dumps({'restored':False,'reason':'Session changed; preserved newer intent','session':SID,'version':s['version']}));return
    before_config=json.loads(s['config'])
    receipt=selections.submit(rt,actor,SID,{'items':[{'kind':'asset','asset_id':a} for a in ASSETS],'version':s['version'],'operation_id':op})
    after=studio.session(rt,actor,SID)
    assert after['config']==before_config
    assert [i['asset_id'] for i in after['selection']['items']]==ASSETS
    assert len(after['runs'])==runs,'Recovery must not create a render intent'
    print(json.dumps({'restored':True,'count':5,'config_preserved':True,'render_intents_unchanged':True,**receipt}))


if __name__=='__main__':main()
