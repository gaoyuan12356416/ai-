import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.deploy_x_personal_publish import database_facts, require_compatible_personal_rollback


class ExistingPersonalTaskUpgradeTests(unittest.TestCase):
    def test_existing_tasks_can_roll_back_but_new_tasks_and_pool_operators_cannot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'posts.sqlite'
            with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript("CREATE TABLE x_video_publish_task(id INTEGER PRIMARY KEY);"
                    "INSERT INTO x_video_publish_task VALUES(1);"
                    "CREATE TABLE x_video_publish_pool(operator_tenant_key TEXT,operator_user_id TEXT);"
                    "INSERT INTO x_video_publish_pool VALUES('','');")
            manifest={'baseline':{'paths':{'post_db':str(path)}},'personal_tasks_before':1}
            require_compatible_personal_rollback(manifest)
            with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                conn.execute('INSERT INTO x_video_publish_task VALUES(2)')
            with self.assertRaisesRegex(RuntimeError,'new personal tasks'):
                require_compatible_personal_rollback(manifest)
            manifest['personal_tasks_before']=2
            with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("UPDATE x_video_publish_pool SET operator_user_id='operator-b'")
            with self.assertRaisesRegex(RuntimeError,'shared pool operator'):
                require_compatible_personal_rollback(manifest)


class UserStartupInvariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'users.sqlite3'
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('CREATE TABLE drama_admin_user (id INTEGER PRIMARY KEY, role TEXT, status TEXT, '
                         'permissions_json TEXT, avatar_url TEXT, updated_at TEXT)')
            conn.execute('INSERT INTO drama_admin_user VALUES (1, ?, ?, ?, ?, ?)',
                         ('admin', 'active', '{"existing": true}', 'same-avatar', 'before'))
            conn.execute('INSERT INTO drama_admin_user VALUES (2, ?, ?, ?, ?, ?)',
                         ('user', 'active', '{"existing": false}', 'same-avatar', 'before'))
        self.before = self.facts()

    def facts(self):
        return database_facts(self.path, tables=['drama_admin_user'], user_startup=True)

    def mutate(self, column, value, user=1):
        with contextlib.closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('UPDATE drama_admin_user SET ' + column + '=? WHERE id=?', (value, user))

    def test_expected_startup_only(self):
        self.mutate('updated_at', 'after')
        self.mutate('updated_at', 'after', 2)
        self.mutate('permissions_json', json.dumps({'existing': True, 'x_video_publish': True}))
        self.assertEqual(self.before, self.facts())

    def test_existing_grants_and_user_grants_remain_guarded(self):
        for permissions, user in [({'existing': False, 'x_video_publish': True}, 1),
                                  ({'existing': True, 'other': True}, 1),
                                  ({'existing': False, 'x_video_publish': True}, 2)]:
            with self.subTest(permissions=permissions, user=user):
                self.mutate('permissions_json', json.dumps(permissions), user)
                self.assertNotEqual(self.before, self.facts())
                self.mutate('permissions_json', '{"existing": true}')
                self.mutate('permissions_json', '{"existing": false}', 2)

    def test_identity_status_and_avatar_remain_guarded(self):
        for column, value, original in [('role', 'user', 'admin'), ('status', 'disabled', 'active'),
                                        ('avatar_url', 'unexpected-avatar', 'same-avatar')]:
            with self.subTest(column=column):
                self.mutate(column, value)
                self.assertNotEqual(self.before, self.facts())
                self.mutate(column, original)

    def test_false_new_admin_grant_is_rejected(self):
        self.mutate('permissions_json', '{"existing": true, "x_video_publish": false}')
        with self.assertRaisesRegex(RuntimeError, 'unexpected personal publishing admin grant'):
            self.facts()


if __name__ == '__main__':
    unittest.main()
