from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock
from features.fb_page_current.common import encoded,pack,sha
from features.fb_page_current.pins import Pins,session
from features.fb_page_current.read import SelectionCache,request

class CurrentRead(unittest.TestCase):
    def test_replicated_new_version_is_not_stored_under_old_hint(self):
        v1='20261123T060000000000Z';v2='20261123T070000000000Z'
        raw=encoded(dict(page_id='101',status='ready',audiences=[])).encode()
        reader=Mock();reader.query.return_value=[dict(version=v2),dict(id='101',value=pack(raw).hex(),hash=sha(raw))]
        cache=SelectionCache()
        self.assertEqual(cache.load(reader,v1,('101',))[1]['version'],v2)
        cache.load(reader,v2,('101',))
        self.assertEqual(reader.query.call_count,1)
        cache.load(reader,v1,('101',))
        self.assertEqual(reader.query.call_count,2)
        self.assertIn('COALESCE',reader.query.call_args[0][0][0])
        with self.assertRaises(RuntimeError):request(reader,v1,['101'],exact=True,detail=True)

    def test_two_tabs_and_logout_preserve_only_needed_page(self):
        with tempfile.TemporaryDirectory() as dirname:
            root=Path(dirname);auth=root/'auth.sqlite3'
            with closing(sqlite3.connect(auth)) as db,db:
                db.execute('CREATE TABLE drama_admin_session(id INTEGER PRIMARY KEY,session_token TEXT,expires_at INTEGER)')
                db.executemany('INSERT INTO drama_admin_session VALUES(?,?,?)',[(i,str(i),int(time.time())+900) for i in (1,2)])
            pins=Pins(root/'pins.sqlite3',auth)
            a=session('drama_admin_session=1',auth);b=session('drama_admin_session=2',auth)
            v='20261123T060000000000Z';body=b'{"page_id":"101"}';meta={'version':v}
            pins.save('101',v,meta,body,a)
            self.assertEqual(pins.get('101',v,b),(body,meta))
            with closing(sqlite3.connect(auth)) as db,db:db.execute('DELETE FROM drama_admin_session WHERE id=1')
            self.assertEqual(pins.clean(),0)
            with self.assertRaises(PermissionError):session('drama_admin_session=1',auth)
            with closing(sqlite3.connect(auth)) as db,db:db.execute('DELETE FROM drama_admin_session WHERE id=2')
            self.assertEqual(pins.clean(),1)
            self.assertIsNone(pins.get('101',v,b))

if __name__=='__main__':unittest.main()

