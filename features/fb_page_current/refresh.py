"""Integrate current-row storage with the existing frozen-input collector and publisher."""
from contextlib import closing
import json
import time
from .common import (MONEY,NEXT,PUBLIC,ROOT,STAGE,STATE,atomic,checked,current_state,digest_rows,encoded,
                     query,read_config,scope_hash,stamp,writer)
from .publish import clear_build,commit,expected_hash,finalize,model_items,owner,promote,verify,wait_visible


def retire_older(reader,db,target,root,obsolete,winner):
    checked(obsolete);checked(winner)
    if obsolete>=winner:raise ValueError('supersession requires a newer committed version')
    wait_visible(reader,f"SELECT HEX(JSON_OBJECT('v',active_version)) FROM {STATE} WHERE singleton=1",lambda r:r[0]['v']==winner)
    head=current_state(reader)
    latest=root/'last-current-proof.json'
    proof=json.loads(latest.read_text()) if latest.exists() else {}
    if proof.get('version')!=winner or proof.get('verified_rows')!=head['pages']:
        keys=[row['p'] for row in query(reader,f"SELECT HEX(JSON_OBJECT('p',page_id)) FROM {target}")]
        rows=digest_rows(reader,target,winner,keys)
        values={pid:[r['calc'],r['summary'],r['a'],r['b'],r['c']] for pid,r in rows.items()}
        amounts={k:sum(r[i+2] for r in values.values()) for i,k in enumerate(MONEY)}
        if (len(rows)!=head['pages'] or scope_hash(rows)!=head['scope'] or expected_hash(values)!=head['manifest']
                or any(r['stored']!=r['calc'] for r in rows.values()) or amounts!=json.loads(head['meta'])['audit']):
            raise RuntimeError('newer committed release reconciliation failed')
        proof=dict(version=winner,table='ads_ai.'+PUBLIC,verified_rows=len(rows),payload_hashes={p:r[0] for p,r in values.items()},**amounts)
        atomic(latest,proof)
    atomic(root/'superseded'/(obsolete+'.json'),dict(at=stamp(),version=obsolete,newer_version=winner,target=target))
    with db.cursor() as c:c.execute(f'DELETE FROM {STAGE} WHERE profile_version=%s',(obsolete,))
    clear_build(root,obsolete)


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
            ack=None
            if actual==pending['base']:
                prior={r['p']:r['h'] for r in query(reader,f"SELECT HEX(JSON_OBJECT('p',page_id,'h',payload_sha256)) FROM {target}")}
                verify(reader,STAGE,v,pending['expected'],base_hashes=prior)
                commit(db,target,v,pending['base'],pending['meta'],pending['expected'])
                ack=(stamp(),time.monotonic())
            elif actual>v:
                retire_older(reader,db,target,root,v,actual)
                file.unlink()
                return
            elif actual!=v:raise RuntimeError('pending receipt conflicts with published base')
            proof=finalize(db,reader,target,pending,root,ack=ack)
            atomic(root/'last-current-proof.json',proof)
            file.unlink()
            clear_build(root,v)


def repair_handoff(reader,env,root):
    mode=read_config()
    if mode not in ('live','live-mirror'):return
    catch_up(reader,env,PUBLIC)
    marker=ROOT/'building.json'
    if marker.exists():
        staged=json.loads(marker.read_text())
        head=current_state(reader)['version']
        if staged['version']<head:
            with owner(ROOT),closing(writer(env)) as db:retire_older(reader,db,PUBLIC,ROOT,staged['version'],head)
        elif staged.get('durable') and head==staged['base']:
            promote(reader,env,PUBLIC,staged['meta'],(),durable_source=True)
    proof=json.loads((ROOT/'last-current-proof.json').read_text())
    file=root/'current.json'
    before=json.loads(file.read_text())
    if before['version']<proof['version']:
        head=current_state(reader)
        if head['version']!=proof['version'] or head['pages']!=proof['verified_rows']:
            raise RuntimeError('unconfirmed pointer repair')
        atomic(root/(proof['version']+'.receipt.json'),proof)
        atomic(root/'previous.json',before)
        atomic(file,dict(version=proof['version'],database='ads_ai.'+PUBLIC,backend='mysql'))
    marker=ROOT/'building.json'
    if marker.exists():
        candidate=json.loads(marker.read_text())
        if candidate['version']==proof['version']:
            clear_build(ROOT,proof['version'])
        elif candidate['target']==PUBLIC and candidate['base']==proof['version']:
            path=root/(candidate['version']+'.input.json')
            if not path.is_file():raise RuntimeError('missing frozen source for interrupted staging')
            return path
        else:raise RuntimeError('unresolved staging receipt')


def primary(data,env,reader,create_table=False):
    from features.fb_page_portrait import storage
    mode=read_config()
    if mode in ('legacy','shadow'):
        return storage.write(data,env,reader,create_table=create_table)
    if create_table:raise ValueError('runtime DDL disabled')
    return promote(reader,env,PUBLIC,data['meta'],model_items(data,storage.projections),durable_source=True)


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
