"""Check the actual MySQL-5.7 mutations and MVCC on a private Unix-socket mysqld."""
from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import tempfile
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import importlib.util
spec=importlib.util.spec_from_file_location('current_operations',Path(__file__).with_name('operations.py'))
ops=importlib.util.module_from_spec(spec);spec.loader.exec_module(ops)
import pymysql
from features.fb_page_current.common import *
from features.fb_page_current.publish import promote
from features.fb_page_current.refresh import catch_up
from features.fb_page_current.schema import install

SOCKET=ROOT/'sandbox-socket/mysql.sock'
def connect(_=None):
    return pymysql.connect(unix_socket=str(SOCKET),user='root',database='ads_ai',autocommit=True,
                           charset='utf8mb4',binary_prefix=True)


def fixture(version,rows):
    amounts={k:sum(x[i] for x in rows.values()) for i,k in enumerate(MONEY,1)}
    meta=dict(version=version,page_count=len(rows),audit=amounts,start='2026-11-14',end='2026-11-20')
    def items():
        for pid,(title,a,b,s,summ) in rows.items():
            detail=encoded({'page_id':pid,'title':title}).encode()
            yield pid,encoded(dict(page_id=pid,s=summ)),detail,sha(detail),[a,b,s],dt.datetime(2026,11,23)
    return meta,items()


def main():
    ops.disk()
    path=Path(tempfile.mkdtemp(dir=ROOT,prefix='qa-'))
    with closing(connect()) as db,db.cursor() as c,closing(connect()) as pinned:
        c.execute('SELECT @@version,@@socket');(version,socket)=c.fetchone()
        if not version.startswith('5.7.') or not socket.endswith('mysql.sock'):raise ValueError('private MySQL required')
        c.execute('SHOW TABLES')
        tables={r[0] for r in c.fetchall()}
        if tables:
            if '--rerun' not in sys.argv or not tables.issubset({PUBLIC,NEXT,LEGACY,STATE,STAGE}) or socket!='/run/portrait-qa/mysql.sock':
                raise RuntimeError('private rehearsal database is not empty')
            for t in sorted(tables):c.execute('DROP TABLE '+t)
        ddl=json.loads((ROOT/'baseline.json').read_text())['ddl']
        c.execute(ddl)
        install(db,(),False)
        rd=ops.ManualReader(db)
        a='20261123T110000000000Z';b='20261123T120000000000Z';d='20261123T130000000000Z'
        first={'101':('同一部剧',-50,0,2,'a'),'102':('removed',0,2,3,'b')}
        later={'101':('同一部剧',-50,0,2,'new summary'),'103':('new',4,5,0,'c')}
        def apply(v,data):
            meta,items=fixture(v,data)
            return promote(rd,{},NEXT,meta,items,root=path,connector=connect)
        assert apply(a,first)['verified_rows']==2
        with pinned.cursor() as q:
            q.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            q.execute(f'SELECT DISTINCT profile_version FROM {NEXT}');assert q.fetchall()==((a,),)
            assert apply(b,later)['verified_rows']==2
            q.execute(f'SELECT DISTINCT profile_version FROM {NEXT}');assert q.fetchall()==((a,),)
            pinned.rollback()
        assert apply(b,later)['verified_rows']==2
        assert apply(d,later)['verified_rows']==2
        fact=json.loads((path/'receipts'/(d+'.json')).read_text());assert fact['detail_bytes_written']==0
        assert json.loads((path/'receipts'/(b+'.json')).read_text())['unchanged']==1
        c.execute(f'SELECT page_id,summary_json FROM {NEXT} ORDER BY page_id')
        output=c.fetchall();assert [x[0] for x in output]==['101','103'] and json.loads(output[0][1])['s']=='new summary'
        try:apply(a,first)
        except RuntimeError as e:assert 'newer' in str(e)
        else:raise AssertionError('old replay accepted')
        e='20261123T140000000000Z'
        with patch('features.fb_page_current.publish.commit',side_effect=ConnectionError('before commit')):
            try:apply(e,first)
            except ConnectionError:pass
            else:raise AssertionError('failure injection')
        assert current_state(rd)['version']==d
        catch_up(rd,{},NEXT,root=path,connector=connect)
        assert current_state(rd)['version']==e
        f='20261123T150000000000Z'
        with patch('features.fb_page_current.publish.finalize',side_effect=ConnectionError('after commit')):
            try:apply(f,later)
            except ConnectionError:pass
            else:raise AssertionError('failure injection')
        catch_up(rd,{},NEXT,root=path,connector=connect)
        assert current_state(rd)['version']==f
        g='20261123T160000000000Z'
        gm,gi=fixture(g,later)
        with patch('features.fb_page_current.publish.stage_rows',side_effect=ConnectionError('partial staging')):
            try:promote(rd,{},NEXT,gm,gi,root=path,connector=connect,durable_source=True)
            except ConnectionError:pass
            else:raise AssertionError('partial staging failure injection')
        from features.fb_page_current.delta import read as read_delta
        assert all(item[2] is None for item in read_delta(path/(g+'.delta.gz')))
        assert promote(rd,{},NEXT,gm,(),root=path,connector=connect,durable_source=True)['version']==g
        assert not (path/'building.json').exists() and not (path/(g+'.delta.gz')).exists()
        from cutover import swap_sql,layout,AFTER,BEFORE
        c.execute(f'INSERT INTO {PUBLIC} SELECT * FROM {NEXT}')
        swap_sql(c,True);assert layout(db)==AFTER
        c.execute(f'SELECT COUNT(*) FROM {PUBLIC} WHERE profile_version=%s',(g,));assert c.fetchone()==(2,)
        swap_sql(c,False);assert layout(db)==BEFORE
        c.execute(f'SELECT COUNT(*) FROM {PUBLIC} WHERE profile_version=%s',(g,));assert c.fetchone()==(2,)
    out=dict(version=version,socket=socket,at=stamp(),tests=['initial','unchanged-detail-new-summary','signed','remove','add','MVCC','same-version-retry','stale-fence','no-change','pre-commit-failure','commit-disconnect','partial-stage-from-frozen-delta','atomic-rename','rollback-rename'])
    atomic(ROOT/'qa/rollback-rehearsed.json',out)
    print(encoded(out),flush=True)

if __name__=='__main__':main()

