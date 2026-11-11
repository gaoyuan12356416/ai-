from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from features.fb_page_portrait.page_list import configured_list,FIELDS
from features.fb_page_portrait.server import fast_pages


class FastListTests(unittest.TestCase):
    def test_current_configuration_and_role_not_cached_authorization(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);users=root/'users.db';pages=root/'pages.db'
            with closing(sqlite3.connect(users)) as db:
                db.execute('CREATE TABLE drama_admin_user(tenant_key,user_id,role,status,name)')
                db.executemany('INSERT INTO drama_admin_user VALUES(?,?,?,?,?)',[('t','admin','admin','active','Admin'),('t','creator','user','active','Creator')]);db.commit()
            with closing(sqlite3.connect(pages)) as db:
                db.execute('CREATE TABLE fb_video_page('+','.join(FIELDS)+',deleted)')
                row={f:'' for f in FIELDS};row.update(tenant_key='t',page_id='11',language='en',created_by='creator',version=3)
                db.execute('INSERT INTO fb_video_page VALUES('+','.join('?' for _ in range(len(FIELDS)+1))+')',[*[row[f] for f in FIELDS],0])
                row.update(tenant_key='other',page_id='22')
                db.execute('INSERT INTO fb_video_page VALUES('+','.join('?' for _ in range(len(FIELDS)+1))+')',[*[row[f] for f in FIELDS],0]);db.commit()
            (root/'page-names.json').write_text(json.dumps(dict(names={'11':'Page A','22':'Page B'},eligible_token_count=999)))
            user=dict(tenant_key='t',user_id='admin')
            result=configured_list(user,root,config_db=pages,user_db=users)
            self.assertEqual(len(result['items']),1)
            p=result['items'][0];self.assertEqual(p['page_name'],'Page A');self.assertEqual(p['created_by_name'],'Creator')
            self.assertIsNone(p['eligible_token_count']);self.assertTrue(p['authorization_pending'])
            with closing(sqlite3.connect(pages)) as db:
                db.execute("UPDATE fb_video_page SET language='',version=4 WHERE page_id='11'");db.commit()
            self.assertEqual(configured_list(user,root,config_db=pages,user_db=users)['items'][0]['language'],'')
            with closing(sqlite3.connect(users)) as db:
                db.execute("UPDATE drama_admin_user SET role='user' WHERE user_id='admin'");db.commit()
            with self.assertRaises(PermissionError):configured_list(user,root,config_db=pages,user_db=users)

    def test_fast_route_requires_cookie_and_existing_navigation_gate(self):
        self.assertEqual(fast_pages('',None)[0],401)
        with patch('features.fb_page_portrait.server.authenticated_get',return_value={'authenticated':True,'user':{'is_admin':False}}):
            self.assertEqual(fast_pages('cookie',None)[0],403)
        with patch('features.fb_page_portrait.server.authenticated_get',side_effect=[{'authenticated':True,'user':{'is_admin':True}}, {'ok':False}]),patch('features.fb_page_portrait.server.configured_list') as read:
            with self.assertRaises(ValueError):fast_pages('cookie',None)
            read.assert_not_called()


if __name__=='__main__':unittest.main()
