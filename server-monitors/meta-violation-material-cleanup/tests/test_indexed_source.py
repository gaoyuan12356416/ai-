"""Exercise all inverse lookup branches against a small relational fixture."""
import json
import pathlib
import re
import sqlite3
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from meta_violation_cleanup.indexed_source import material_rows
from meta_violation_cleanup.sql_source import SqlSource
from meta_violation_cleanup.policy import material_identity


class FixtureSource:
    schema='main';page_size=2
    _select=SqlSource._select
    def __init__(self):
        self.db=sqlite3.connect(':memory:')
        self.db.create_function('LOCATE',2,lambda needle,value: (value or '').find(needle)+1)
        self.db.create_function('REGEXP',2,lambda pattern,value:bool(re.search(pattern.replace('[[:space:]]',r'\s'),value or '')))
        self.db.execute('CREATE TABLE ads_source(id INTEGER PRIMARY KEY,source_type INTEGER,source_id TEXT)')
        self.db.execute('CREATE TABLE ads_facebook_auto_created_data(id INTEGER PRIMARY KEY,'+
            ','.join(c+' TEXT' for c in ('ad_id','product','source_id','original_source_id','creative_id',
                                      'ad_account_id','user_id','publish_queue_id','video_id','image_ids'))+')')
        self.queries=[]

    @contextmanager
    def _session(self):yield self

    def query(self,sql):
        self.queries.append(sql)
        sql=re.sub(r' FORCE INDEX\([A-Za-z0-9_]+\)','',sql)
        sql=re.sub(r'_utf8mb4 0x([0-9a-fA-F]+)',lambda m:"CAST(X'"+m[1]+"' AS TEXT)",sql)
        sql=re.sub(r'LEFT\(TRIM\((\w+)\),1\)',r'substr(TRIM(\1),1,1)',sql)
        return [json.loads(r[0]) for r in self.db.execute(sql)]


class IndexedSourceTests(unittest.TestCase):
    def test_inverse_covers_scalar_empty_zero_csv_json_and_whitespace(self):
        source=FixtureSource()
        aliases=[(101,3,'7'),(102,3,'[7,8]'),(103,3,'\t7\n'),(104,3,'8')]
        source.db.executemany('INSERT INTO ads_source VALUES(?,?,?)',aliases)
        # Every valid representation that intersects material 7 is recoverable;
        # false positives are subsequently rejected by material_identity.
        representations=[('101','7'),('101',None),('101','0'),('101','[]'),
                         ('101,104','7,8'),('[101,104]','[7,8]'),('102',None),
                         ('103',None),('101','\t7\n'),(' 101 ',' 7 '),
                         ('104','8'),('101','9')]
        rows=[]
        for n,(sid,original) in enumerate(representations,1):
            row={'id':str(n),'ad_id':str(n+1000),'product':'1479','source_id':sid,
                 'original_source_id':original,'creative_id':str(n+2000),
                 'ad_account_id':'100','user_id':'1','publish_queue_id':'1','video_id':'','image_ids':''}
            rows.append(row)
            source.db.execute('INSERT INTO ads_facebook_auto_created_data VALUES('+','.join('?' for _ in row)+')',list(row.values()))
        records,metrics=material_rows(source,{'7'},{'1479':'1479'})
        found={r['id'] for r in records};mapping={str(i):{'id':str(i),'source_type':t,'source_id':s} for i,t,s in aliases}
        expected={r['id'] for r in rows if (lambda m:not m['reason'] and '7' in m['material_ids'])(
            material_identity([r],mapping,{'1479':'1479'},r['creative_id']))}
        self.assertEqual(expected,{str(n) for n in range(1,11)})
        self.assertTrue(expected<=found,expected-found)
        self.assertEqual(metrics['publication_rows_examined_for_composites'],len(rows))
        self.assertTrue(any('AS bounded_ids' in q for q in source.queries))
        source.db.close()


if __name__=='__main__':unittest.main()
