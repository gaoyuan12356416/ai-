from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from features.moboreels_library.actors import resolve_actor
from features.moboreels_library.promotions import PromotionError


class CreatorResolutionTest(unittest.TestCase):
    def test_only_frozen_unique_active_creator_can_resolve(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'users.sqlite'
            with sqlite3.connect(path) as c:
                c.execute('CREATE TABLE drama_admin_user(user_id TEXT,tenant_key TEXT,name TEXT,status TEXT)')
                c.executemany('INSERT INTO drama_admin_user VALUES(?,?,?,?)',[
                    ('creator','tenant','创建者','active'),('disabled','tenant','停用用户','disabled'),
                    ('ambiguous','one','用户一','active'),('ambiguous','two','用户二','active')])
            c.close()
            settings=SimpleNamespace(job_db_path=str(path),institution_user_id='institution')
            self.assertEqual(resolve_actor(settings,'creator'),{'tenant_key':'tenant','user_id':'creator','name':'创建者'})
            for uid in ('disabled','ambiguous','unknown',''):
                with self.assertRaises(PromotionError):resolve_actor(settings,uid)
            with self.assertRaises(PromotionError):resolve_actor(settings,'creator','other-tenant')


if __name__=='__main__':unittest.main()
