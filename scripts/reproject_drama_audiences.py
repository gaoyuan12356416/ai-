"""Preview and publish conserved X/FB audience projections; no publishing writes."""
import argparse
from contextlib import closing,ExitStack
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from features.drama_audience.reproject import project
from features.drama_audience.store import VERSION

X=Path('/mnt/data-disk/x-channel-tag-revenue')
FB=Path('/mnt/data-disk/fb-page-portraits')


def save(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),encoding='utf8')


def read_fb():
    manifest=json.loads((FB/'current.json').read_text());profiles={}
    with closing(sqlite3.connect((FB/manifest['database']).as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        meta=json.loads(db.execute('SELECT payload FROM metadata').fetchone()[0])
        for pid,text in db.execute('SELECT page_id,tree FROM profiles'):
            p=json.loads(text)
            for a in p['audiences']:
                for t in a['children']:
                    t['dramas']=[json.loads(r[0]) for r in db.execute('SELECT payload FROM dramas WHERE page_id=? AND path=? ORDER BY position',(pid,a['key']+'/'+t['key']))]
            profiles[pid]=p
    meta['audit']={k:sum(p[k] for p in profiles.values()) for k in ('paid_cents','social_cents','spend_cents')}
    meta['page_count']=len(profiles)
    return dict(meta=meta,pages=profiles),manifest


def preview(folder):
    pointer=json.loads((X/'current.json').read_text());oldx=json.loads((X/pointer['file']).read_text())
    oldfb,fbpointer=read_fb()
    newx,xproof=project(oldx['accounts']);newfb,fbproof=project(oldfb['pages'])
    for kind,old,rows,proof,key in [('x',oldx,newx,xproof,'accounts'),('fb',oldfb,newfb,fbproof,'pages')]:
        save(folder/(kind+'-before.json'),old)
        data={**old,key:rows,'meta':dict(old['meta'])}
        stamp=datetime.now(timezone.utc)
        data['meta'].update(version=stamp.strftime('%Y%m%dT%H%M%S%fZ'),generated_at=stamp.isoformat(),
            audience_source='material_list2.audience_labels',audience_taxonomy_version=VERSION,
            taxonomy_version=VERSION,projection_only=True)
        save(folder/(kind+'-next.json'),data);save(folder/(kind+'-diff.json'),proof)
    save(folder/'source-pointers.json',dict(x=pointer,fb=fbpointer))
    summary={k:{n:v for n,v in p.items() if n not in ('changes','missing')} for k,p in [('x',xproof),('fb',fbproof)]}
    summary.update(missing_ids=sorted(set(xproof['missing'])|set(fbproof['missing'])))
    save(folder/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return summary


def publish(folder):
    from features.social_analytics.warehouse_refresh import Reader,environment,atomic_json
    from features.x_tag_revenue.storage import ProfileWriter
    from features.x_tag_revenue.serving import publish as publish_x
    import features
    fbroot=Path('/opt/fb-page-portraits/current')
    features.__path__.append(str(fbroot/'features'))
    from features.fb_page_portrait.storage import write as write_fb,publish as publish_fb
    env=environment('/root/drama_material_service/.env');reader=Reader(env)
    pointers=json.loads((folder/'source-pointers.json').read_text())
    assert json.loads((X/'current.json').read_text())==pointers['x'],'x_source_changed'
    assert json.loads((FB/'current.json').read_text())==pointers['fb'],'fb_source_changed'
    data=json.loads((folder/'x-next.json').read_text())
    with closing(ProfileWriter(env)) as writer:proof=writer.publish(data,reader)
    publish_x(X,data,proof)
    filename='snapshots/'+data['meta']['version']+'.json'
    atomic_json(X/filename,data);atomic_json(X/'current.json',dict(file=filename,version=data['meta']['version'],previous=pointers['x']['file']))
    atomic_json(X/'status.json',dict(status='ok',attempted_at=data['meta']['generated_at']))
    save(folder/'x-publish-proof.json',proof)
    data=json.loads((folder/'fb-next.json').read_text())
    proof=write_fb(data,env,reader);publish_fb(data,proof,FB)
    # Keep the original raw facts available for the next incremental discovery.
    original=FB/(pointers['fb']['version']+'.input.json')
    if original.exists():
        import shutil
        shutil.copy2(original,FB/(data['meta']['version']+'.input.json'))
    save(folder/'fb-publish-proof.json',proof)
    atomic_json(FB/'status.json',dict(status='ok',**data['meta']))


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['preview','publish']);p.add_argument('--folder',type=Path,required=True);args=p.parse_args()
    args.folder.mkdir(parents=True,exist_ok=True)
    if args.action=='preview':preview(args.folder)
    else:
        with ExitStack() as stack:
            for root in (X,FB):
                handle=stack.enter_context((root/'refresh.lock').open('a'));fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            # Re-pin current financial facts, then prove conservation before publishing.
            preview(args.folder);publish(args.folder)


if __name__=='__main__':main()
