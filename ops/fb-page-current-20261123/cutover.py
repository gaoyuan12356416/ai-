"""Switch just the portrait namespace and owner; reconcile ambiguous DDL by identity."""
import argparse
import time
from contextlib import closing,contextmanager
from operations import *
from features.fb_page_current.publish import owner
from features.fb_page_current.common import read_config


@contextmanager
def refresh_lock():
    with (PORTRAITS/'refresh.lock').open('a') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('refresh in progress; retry after its natural completion') from None
        with owner(ROOT):yield


def mode(label):
    atomic(ROOT/'mode.json',dict(mode=label,at=stamp()))


def layout(db):
    with db.cursor() as c:
        return [shape(c,t) for t in (PUBLIC,NEXT,LEGACY)]


ORIG=('profile_version','page_id')
HOT=('page_id',)
BEFORE=[ORIG,HOT,()]
AFTER=[HOT,(),ORIG]


def natural(v,expected_mode):
    o=json.loads((ROOT/'comparisons'/(v+'.json')).read_text())
    if (o.get('version')!=v or o.get('mode')!=expected_mode or o.get('outcome')!='matched'
        or o.get('resumed') or not o.get('invocation')):raise RuntimeError('matching natural refresh receipt required')
    return o


def swap_sql(c,forward):
    if forward:c.execute(f'RENAME TABLE {PUBLIC} TO {LEGACY}, {NEXT} TO {PUBLIC}')
    else:c.execute(f'RENAME TABLE {PUBLIC} TO {NEXT}, {LEGACY} TO {PUBLIC}')


def confirm_shape(db,desired):
    limit=time.monotonic()+100
    while layout(db)!=desired:
        if time.monotonic()>limit:raise RuntimeError('DDL acknowledgement pending on replica')
        time.sleep(1)


def transition(forward):
    cfg=env()
    with refresh_lock(),closing(connect_reader(cfg)) as db:
        expected,desired=(BEFORE,AFTER) if forward else (AFTER,BEFORE)
        action='cutover' if forward else 'rollback'
        intent_file=ROOT/'switch-intent.json'
        previous='shadow' if forward else 'live-mirror'
        v=current();rd=ManualReader(db)
        if (ROOT/'pending.json').exists():raise RuntimeError('pending current-row publish')
        if current_state(rd)['version']!=v:raise RuntimeError('source and current table versions differ')
        with db.cursor() as c:
            if layout(db)==expected:
                facts=json.loads((ROOT/'qa'/('compare-'+(NEXT if forward else PUBLIC)+'.json')).read_text())
                if facts['version']!=v:raise RuntimeError('run the focused field and selection comparison for the current version')
                if forward:
                    natural(v,'shadow')
                    for name in ('readers-live.json','qa/pin-verified.json','qa/rollback-rehearsed.json'):
                        if not (ROOT/name).exists():raise RuntimeError('missing '+name)
                else:
                    count=one(c,f'SELECT COUNT(*) FROM {LEGACY} WHERE profile_version=%s',(v,))[0]
                    if count!=receipt(v)['verified_rows']:raise RuntimeError('rollback table has not caught up')
                if intent_file.exists():
                    old=json.loads(intent_file.read_text())
                    if (old['action'],old['version'])!=(action,v):raise RuntimeError('conflicting DDL intent')
                else:atomic(intent_file,dict(action=action,version=v,at=stamp()))
                # Both regular writers reject an unresolved DDL mode, leaving the published reader available.
                mode('switching')
                try:
                    with closing(writer(cfg)) as dbw,dbw.cursor() as wc:swap_sql(wc,forward)
                except pymysql.MySQLError as e:
                    if e.args[0] in (1205,1213) and layout(db)==expected:
                        mode(previous);intent_file.unlink()
                    raise
            elif layout(db)==desired:
                if not intent_file.exists():
                    done=ROOT/(action+'.json')
                    if done.exists():return json.loads(done.read_text())
                    raise RuntimeError('unexpected layout without matching switch intent')
                intent=json.loads(intent_file.read_text())
                if intent['action']!=action or intent['version']!=v:raise RuntimeError('DDL recovery identity mismatch')
            else:raise RuntimeError('unrecognized table arrangement')
            confirm_shape(db,desired)
            n=one(c,f'SELECT COUNT(*) FROM {PUBLIC} WHERE profile_version=%s',(v,))[0]
            if n!=receipt(v)['verified_rows']:raise RuntimeError('public version incomplete after rename')
            if forward:
                direct=one(c,f'SELECT COUNT(*),COUNT(DISTINCT page_id),COUNT(DISTINCT profile_version) FROM {PUBLIC}')
                if direct!=(n,n,1):raise RuntimeError('current table uniqueness after rename')
            mode('live-mirror' if forward else 'legacy')
            fact=dict(at=stamp(),version=v,pages=n,layout=desired,action=action)
            atomic(ROOT/(action+'.json'),fact);intent_file.unlink(missing_ok=True)
            return fact


def freeze():
    with refresh_lock(),closing(connect_reader(env())) as db,db.cursor() as c:
        v=current()
        if read_config()=='live' and (ROOT/'history-frozen.json').exists():return json.loads((ROOT/'history-frozen.json').read_text())
        if read_config()!='live-mirror' or layout(db)!=AFTER:raise RuntimeError('wrong observation phase')
        mark=json.loads((ROOT/'cutover.json').read_text())
        if v<=mark['version']:raise RuntimeError('post-cut natural cycle not observed')
        natural(v,'live-mirror')
        facts=json.loads((ROOT/'qa'/('compare-'+PUBLIC+'.json')).read_text())
        if facts['version']!=v:raise RuntimeError('post-cut comparison is not current')
        before=json.loads((ROOT/'qa/metrics-before.json').read_text());after=json.loads((ROOT/'qa/metrics-after.json').read_text())
        if before['recovery_pid']!=after['recovery_pid']:raise RuntimeError('review unrelated recovery worker change')
        c.execute(f'SELECT DISTINCT profile_version FROM {LEGACY} ORDER BY profile_version')
        versions=[x[0] for x in c.fetchall()]
        if v!=versions[-1]:raise RuntimeError('legacy high-water mismatch')
        c.execute('SELECT table_name,data_length,index_length FROM information_schema.tables '
                  'WHERE table_schema=DATABASE() AND table_name IN (%s,%s,%s,%s)',(PUBLIC,LEGACY,STATE,STAGE))
        sizes=c.fetchall()
        fact=dict(at=stamp(),version=v,versions=versions,public_rows=facts['rows'],sizes=sizes,
                  total_schema_bytes=one(c,'SELECT SUM(data_length+index_length) FROM information_schema.tables WHERE table_schema=DATABASE()')[0])
        fact['total_schema_bytes']=int(fact['total_schema_bytes'])
        atomic(ROOT/'history-frozen.json',fact);mode('live')
        return fact


if __name__=='__main__':
    ps=argparse.ArgumentParser();ps.add_argument('action',choices=['cutover','rollback','freeze'])
    arg=ps.parse_args().action;disk()
    result=freeze() if arg=='freeze' else transition(arg=='cutover')
    print(encoded({k:v for k,v in result.items() if k!='versions'}),flush=True)

