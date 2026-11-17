"""One-time additive import through the existing trusted Page management API."""
import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import urllib.request

from .domain import encoded,utcnow
from .sources import environment,shared,Sources

CONFIG_DB='/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'


def atomic(path,value):
    path=Path(path);temp=path.with_suffix('.next');temp.write_text(encoded(value),encoding='utf-8');temp.replace(path)


def identity(env):
    request=urllib.request.Request('http://127.0.0.1:8787/internal/fb-video-publish/identity',
        data=encoded(dict(tenant_key='149bb4a14b57975f',user_id='892fd2e8')).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+env['FB_AUTO_POST_INTERNAL_TOKEN']},method='POST')
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=30) as response:
        actor=json.load(response)['actor']
    if actor.get('user_id')!='892fd2e8' or not actor.get('is_admin'):raise RuntimeError('trusted_import_actor_mismatch')
    return actor


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();env=environment();shared(env)
    from features.fb_video_publish.client import request,GatewayError
    for line in Path('/etc/fb-auto-post-app.env').read_text().splitlines():
        if '=' in line and line.startswith('FB_AUTO_POST_'):
            k,v=line.split('=',1);env[k]=v.strip().strip('"').strip("'")
    actor=identity(env)
    pages=Sources(env).pages()
    before=request('GET','/api/fb-video-publish/pages',actor=actor,environ=env)['items']
    existing={str(p['page_id']):p for p in before}
    with sqlite3.connect('file:'+CONFIG_DB+'?mode=ro',uri=True) as db:
        deleted={str(row[0]) for row in db.execute('SELECT page_id FROM fb_video_page WHERE deleted=1 AND tenant_key=?',(actor['tenant_key'],))}
    templates=defaultdict(set)
    for row in before:
        if row.get('created_by')==actor['user_id']:
            templates[str(row.get('language') or '')].add((row.get('caption_template') or '',row.get('title') or ''))
    def template(lang):
        values=templates.get(lang) or templates.get('en')
        if not values or len(values)!=1:raise RuntimeError('default_template_not_unique_'+lang)
        return next(iter(values))
    additions={str(p['page_id']):p for p in pages if p['status']==0 and str(p['page_id']) not in existing and str(p['page_id']) not in deleted}
    report=dict(at=utcnow(),before=len(before),new=len(additions),groups=dict(Counter(str(p['group_id']) for p in additions.values())),
                preserved_deleted=sorted({str(p['page_id']) for p in pages if p['status']==0 and str(p['page_id']) in deleted}),
                added=[],failed=[],unknown=[])
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic(output.with_suffix('.preview.json'),dict(report,members=list(additions.values())))
    if not args.apply:print(encoded(report));return
    backup=output.with_suffix('.before.sqlite3')
    if backup.exists():raise RuntimeError('import_backup_exists_use_new_receipt_and_current_state')
    with sqlite3.connect('file:'+CONFIG_DB+'?mode=ro',uri=True) as src,sqlite3.connect(backup) as dest:src.backup(dest)
    atomic(output.with_suffix('.before.json'),before)
    groups=defaultdict(list)
    for pid,page in additions.items():groups[str(page.get('language') or '').strip().lower().replace('_','-')].append(pid)
    def save(ids,lang):
        caption,title=template(lang)
        try:
            request('POST','/api/fb-video-publish/pages',actor=actor,environ=env,
                    payload=dict(page_ids=ids,language=lang,caption_template=caption,title=title))
            report['added'].extend(ids)
        except GatewayError as exc:
            if exc.accepted is None:
                # Reconcile later; do not overwrite a concurrently created Page
                # or blindly reissue an RPC with an unknown outcome.
                report['unknown'].extend(ids)
            elif len(ids)>1:
                middle=len(ids)//2;save(ids[:middle],lang);save(ids[middle:],lang)
            else:report['failed'].append(dict(page_id=ids[0],reason=exc.code))
        atomic(output,report)
    for lang,ids in sorted(groups.items()):
        for offset in range(0,len(ids),100):save(ids[offset:offset+100],lang)
        print(encoded(dict(event='page_import_language',language=lang,completed=len(report['added']),failed=len(report['failed']),unknown=len(report['unknown']))),flush=True)
    after=request('GET','/api/fb-video-publish/pages',actor=actor,environ=env)['items']
    actual={str(row['page_id']):row for row in after}
    fields=('page_id','config_instance_id','version','language','caption_template','title','created_by','updated_by','created_at_utc','updated_at_utc')
    for pid,row in existing.items():
        if pid not in actual or any(actual[pid].get(k)!=row.get(k) for k in fields):raise RuntimeError('preexisting_page_configuration_changed_'+pid)
    expected={pid for pid in additions}
    verified=[]
    for pid in expected & actual.keys():
        page=actual[pid];lang=str(additions[pid].get('language') or '').strip().lower().replace('_','-')
        caption,title=template(lang)
        if (page.get('created_by')==actor['user_id'] and page.get('language')==lang
            and page.get('caption_template')==caption and (page.get('title') or '')==title):verified.append(pid)
    report.update(after=len(after),verified=sorted(verified),unverified=sorted(expected-set(verified)),backup=str(backup))
    atomic(output,report)
    print(encoded({k:v for k,v in report.items() if k not in {'added','verified'}}))
    if report['unverified']:raise SystemExit(2)


if __name__=='__main__':main()
