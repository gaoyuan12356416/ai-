"""Integrate current-row storage with the existing frozen-input collector and publisher."""
from contextlib import closing
import json
from .common import (LEGACY,MONEY,NEXT,PORTRAITS,PUBLIC,ROOT,STAGE,atomic,current_state,encoded,
                     query,read_config,stamp,writer)
from .publish import commit,finalize,model_items,owner,promote,verify


def catch_up(reader,env,target,root=ROOT,connector=writer):
    file=root/'pending.json'
    with owner(root):
        if not file.exists():return
        pending=json.loads(file.read_text())
        if pending['target']!=target:raise RuntimeError('pending table needs migration reconciliation')
        v=pending['meta']['version']
        with closing(connector(env)) as db:
            with db.cursor() as c:
                # A one-row write fence distinguishes commit-disconnect from replica delay.
                c.execute('SELECT active_version FROM fb_page_revenue_profile_state WHERE singleton=1 FOR UPDATE')
                actual=c.fetchone()[0]
            if actual==pending['base']:
                prior={r['p']:r['h'] for r in query(reader,f"SELECT HEX(JSON_OBJECT('p',page_id,'h',payload_sha256)) FROM {target}")}
                verify(reader,STAGE,v,pending['expected'],base_hashes=prior)
                commit(db,target,v,pending['base'],pending['meta'],pending['expected'])
            elif actual!=v:raise RuntimeError('pending receipt conflicts with newer head')
            proof=finalize(db,reader,target,pending,root)
            atomic(root/'last-current-proof.json',proof)
            file.unlink()


def repair_handoff(reader,env,root):
    mode=read_config()
    if mode not in ('live','live-mirror'):return
    catch_up(reader,env,PUBLIC)
    proof=json.loads((ROOT/'last-current-proof.json').read_text())
    file=root/'current.json'
    before=json.loads(file.read_text())
    if before['version']>=proof['version']:return
    head=current_state(reader)
    if head['version']!=proof['version'] or head['pages']!=proof['verified_rows']:
        raise RuntimeError('unconfirmed pointer repair')
    atomic(root/(proof['version']+'.receipt.json'),proof)
    atomic(root/'previous.json',before)
    atomic(file,dict(version=proof['version'],database='ads_ai.'+PUBLIC,backend='mysql'))


def primary(data,env,reader,create_table=False):
    from features.fb_page_portrait import storage
    mode=read_config()
    if mode in ('legacy','shadow'):
        return storage.write(data,env,reader,create_table=create_table)
    if create_table:raise ValueError('runtime DDL disabled')
    return promote(reader,env,PUBLIC,data['meta'],model_items(data,storage.projections))


def secondary(data,env,reader,proof,*,resumed=False):
    from .mirror import seal
    mode=read_config()
    if mode not in ('live-mirror','shadow'):return True
    version=proof['version']
    try:
        seal(data,proof,mode,resumed)
        print(encoded(dict(event='portrait_mirror_queued',version=version,mode=mode)),flush=True)
        return True
    except Exception as exc:
        atomic(ROOT/'comparisons'/(version+'.error.json'),dict(version=version,mode=mode,at=stamp(),error=type(exc).__name__,message=str(exc)[:250]))
        print(encoded(dict(event='portrait_secondary_deferred',version=version,mode=mode,error=type(exc).__name__)),flush=True)
        return False
