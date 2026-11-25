import os
import sqlite3
import unittest
from unittest.mock import patch

if os.name == 'posix':
    from scripts import fb_catalog_refresh as refresh


@unittest.skipUnless(os.name=='posix','the gated catalog refresher runs on Linux')
class ReconciliationTests(unittest.TestCase):
    def test_edited_removed_and_new_materials(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE material(id INTEGER PRIMARY KEY,cid,lang,product,duration,url,name,tag)')
        def material(i,name):
            return (i,'c','en','Dramawave',10,'https://example.org/',name,'')
        db.executemany('INSERT INTO material VALUES(?,?,?,?,?,?,?,?)',[material(1,'keep'),material(2,'old'),material(3,'remove')])
        class Reader:
            def query(self, statements):
                sql=statements[0]
                if 'MAX(id)' in sql:return [[4]]
                if 'GROUP_CONCAT' in sql:return [['1,2,4','2']]
                self.sql=sql
                return [material(2,'edit'),material(4,'new')]
        reader=Reader()
        refresh.refresh_materials(db,reader,10)
        self.assertIn('id IN (2,4)',reader.sql)
        self.assertEqual(db.execute('SELECT id,name FROM material ORDER BY id').fetchall(),[(1,'keep'),(2,'edit'),(4,'new')])

    def test_resource_deletion_and_changed_content(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE material(cid)')
        db.executemany('INSERT INTO material VALUES(?)',[(c,) for c in 'abc'])
        db.execute('CREATE TABLE resource(cid PRIMARY KEY,n,items)')
        db.executemany('INSERT INTO resource VALUES(?,?,?)',[('a',2,'[]'),('b',1,'[]'),('c',1,'unchanged'),('gone',1,'[]')])
        class Reader:
            def query(self,statements,**kwargs):
                sql=statements[0]
                if 'SELECT DISTINCT drama_status FROM' in sql:return [[0]]
                if 'INDEX(du)' in sql:return [['b']]
                if 'COUNT(*)' in sql:return [['a',1],['b',1],['c',1]]
                return [['a','1479','en','s','','fresh'],['b','1479','en','s','','changed']]
        with patch.object(refresh,'resource_apps',return_value=['android']),patch.object(refresh.time,'time',return_value=11):
            refresh.refresh_resources(db,Reader(),10)
        rows=dict(db.execute('SELECT cid,items FROM resource'))
        self.assertEqual(set(rows),set('abc'))
        self.assertEqual(rows['c'],'unchanged')
        self.assertIn('fresh',rows['a'])
        self.assertIn('changed',rows['b'])
