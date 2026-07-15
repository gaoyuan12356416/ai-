import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from deploy import migrate_ad_control_account_copy_v2_sqlite as migration


GROUPS = {
    "frg_plus8_non_asian_lang_10am_dramawave_binding": ("dramawave", 1, 0, 0),
    "frg_plus8_non_asian_lang_10am_freereels_binding": ("freereels", 0, 0, 1),
    "frg_plus8_non_asian_lang_10am_hotdrama_binding": ("hotdrama", 0, 0, 1),
}


class OwnerMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ad-control-owner-migration-")
        self.root = Path(self.temporary.name)
        self.db = self.root / "jobs.sqlite3"
        self.app = self.root / "app.py"
        self.app.write_text("# test app path\n", encoding="utf-8")
        self._create_legacy_database()

    def tearDown(self):
        self.temporary.cleanup()

    def _create_legacy_database(self):
        conn = sqlite3.connect(str(self.db))
        conn.executescript("""
        CREATE TABLE ad_control_rule_group (
          group_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
          product TEXT NOT NULL DEFAULT '', account_group_id TEXT NOT NULL DEFAULT '',
          account_ids_json TEXT NOT NULL DEFAULT '[]', rules_json TEXT NOT NULL DEFAULT '[]',
          enabled INTEGER NOT NULL DEFAULT 0, emergency_stopped INTEGER NOT NULL DEFAULT 0,
          last_preview_id TEXT NOT NULL DEFAULT '', last_preview_hash TEXT NOT NULL DEFAULT '',
          last_run_at TEXT NOT NULL DEFAULT '', last_result_json TEXT NOT NULL DEFAULT '{}',
          created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE ad_control_account_group (
          group_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
          product TEXT NOT NULL DEFAULT '', account_ids_json TEXT NOT NULL DEFAULT '[]',
          created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE ad_control_rule (
          rule_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
          created_by TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        for index, (group_id, state) in enumerate(sorted(GROUPS.items()), 1):
            product = state[0]
            pool_id = group_id.replace("_binding", "_accounts")
            conn.execute(
                "INSERT INTO ad_control_account_group "
                "(group_id,name,product,account_ids_json,created_by,deleted) "
                "VALUES (?,?,?,?,?,0)",
                (pool_id, pool_id, product, '[\"%s\"]' % index, "codex"),
            )
            conn.execute(
                "INSERT INTO ad_control_rule_group "
                "(group_id,name,product,account_group_id,rules_json,enabled,emergency_stopped,created_by,deleted) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    group_id, group_id, product, pool_id,
                    '[{\"rule_id\":\"rule-%s\",\"action\":\"pause\"}]' % index,
                    state[1], state[2], "codex", state[3],
                ),
            )
        conn.commit()
        conn.close()

    @staticmethod
    def fake_ensure(_app_path, db_path):
        conn = sqlite3.connect(str(db_path))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ad_control_rule_group)")}
        if "owner_user_id" not in columns:
            conn.execute(
                "ALTER TABLE ad_control_rule_group "
                "ADD COLUMN owner_user_id TEXT NOT NULL DEFAULT ''"
            )
        if "rule_set_id" not in columns:
            conn.execute(
                "ALTER TABLE ad_control_rule_group "
                "ADD COLUMN rule_set_id TEXT NOT NULL DEFAULT ''"
            )
        if "strategy_json" not in columns:
            conn.execute(
                "ALTER TABLE ad_control_rule_group "
                "ADD COLUMN strategy_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "object_level" not in columns:
            conn.execute(
                "ALTER TABLE ad_control_rule_group "
                "ADD COLUMN object_level TEXT NOT NULL DEFAULT 'campaign'"
            )
        if "run_mode" not in columns:
            conn.execute(
                "ALTER TABLE ad_control_rule_group "
                "ADD COLUMN run_mode TEXT NOT NULL DEFAULT 'live'"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_control_rule_set (
              rule_set_id TEXT PRIMARY KEY, product TEXT NOT NULL DEFAULT '',
              rules_json TEXT NOT NULL DEFAULT '[]', created_by TEXT NOT NULL DEFAULT '',
              deleted INTEGER NOT NULL DEFAULT 0
            )
        """)
        rows = conn.execute(
            "SELECT group_id,product,rules_json,created_by FROM ad_control_rule_group"
        ).fetchall()
        for group_id, product, rules_json, created_by in rows:
            rule_set_id = "legacy_%s" % group_id
            conn.execute(
                "INSERT OR IGNORE INTO ad_control_rule_set "
                "(rule_set_id,product,rules_json,created_by,deleted) VALUES (?,?,?,?,0)",
                (rule_set_id, product, rules_json, created_by),
            )
            conn.execute(
                "UPDATE ad_control_rule_group SET rule_set_id=? WHERE group_id=?",
                (rule_set_id, group_id),
            )
        conn.execute(
            "UPDATE ad_control_rule_group SET owner_user_id=created_by "
            "WHERE owner_user_id='' AND created_by<>''"
        )
        conn.commit()
        conn.close()

    def migrate(self, apply=True):
        if apply:
            return migration.migrate_database(
                self.db, self.app, "892fd2e8", "codex", GROUPS,
                ensure_schema=self.fake_ensure,
            )
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(dir=str(self.root)) as value:
            rehearsal = Path(value) / "rehearsal.sqlite3"
            migration.backup_sqlite(self.db, rehearsal)
            result = migration.migrate_database(
                rehearsal, self.app, "892fd2e8", "codex", GROUPS,
                ensure_schema=self.fake_ensure,
            )
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        return result

    def test_dry_run_rehearses_on_copy_without_changing_source(self):
        result = migration.run_release(
            self.db, self.app, "892fd2e8", "codex", GROUPS, False,
            ensure_schema=self.fake_ensure,
        )
        self.assertEqual("check", result["mode"])
        self.assertEqual(3, result["updated_count"])
        conn = sqlite3.connect(str(self.db))
        self.assertNotIn(
            "owner_user_id",
            {row[1] for row in conn.execute("PRAGMA table_info(ad_control_rule_group)")},
        )
        conn.close()

    def test_apply_updates_exact_three_then_is_idempotent(self):
        first = self.migrate()
        second = self.migrate()
        self.assertEqual(3, first["updated_count"])
        self.assertEqual(0, second["updated_count"])
        conn = sqlite3.connect(str(self.db))
        rows = conn.execute(
            "SELECT owner_user_id,created_by,enabled,emergency_stopped,deleted "
            "FROM ad_control_rule_group ORDER BY group_id"
        ).fetchall()
        conn.close()
        self.assertEqual(["892fd2e8"], sorted({row[0] for row in rows}))
        self.assertEqual(["codex"], sorted({row[1] for row in rows}))
        self.assertEqual([(1, 0, 0), (0, 0, 1), (0, 0, 1)], [
            (row[2], row[3], row[4]) for row in rows
        ])

    def test_real_target_app_ensure_routes_db_and_preserves_owner_semantics(self):
        import app as target_app

        original_db_path = target_app.JOB_DB_PATH
        try:
            result = migration.migrate_database(
                self.db,
                Path(target_app.__file__).resolve(),
                "892fd2e8",
                "codex",
                GROUPS,
            )
            self.assertEqual(3, result["updated_count"])
            target_app.JOB_DB_PATH = str(self.db)
            owner_items = target_app.list_ad_control_rule_groups(
                owner_user_id="892fd2e8"
            )["items"]
            codex_items = target_app.list_ad_control_rule_groups(
                owner_user_id="codex"
            )["items"]
            internal_items = target_app.list_ad_control_rule_groups(
                internal=True
            )["items"]
        finally:
            target_app.JOB_DB_PATH = original_db_path

        self.assertEqual(
            ["frg_plus8_non_asian_lang_10am_dramawave_binding"],
            [item["group_id"] for item in owner_items],
        )
        self.assertEqual([], codex_items)
        self.assertEqual(1, len(internal_items))
        self.assertEqual("892fd2e8", internal_items[0]["owner_user_id"])
        self.assertTrue(internal_items[0]["account_ids"])

    def test_mixed_owner_and_bad_pool_fail_without_owner_update(self):
        self.fake_ensure(self.app, self.db)
        conn = sqlite3.connect(str(self.db))
        first_id = sorted(GROUPS)[0]
        conn.execute(
            "UPDATE ad_control_rule_group SET owner_user_id='foreign' WHERE group_id=?",
            (first_id,),
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "mixed|foreign"):
            self.migrate()

        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "UPDATE ad_control_rule_group SET owner_user_id='codex'"
        )
        pool_id = conn.execute(
            "SELECT account_group_id FROM ad_control_rule_group WHERE group_id=?",
            (first_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE ad_control_account_group SET product='wrong' WHERE group_id=?",
            (pool_id,),
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "product mismatch"):
            self.migrate()

        active_id = "frg_plus8_non_asian_lang_10am_dramawave_binding"
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "UPDATE ad_control_account_group SET product='dramawave' WHERE group_id=?",
            (pool_id,),
        )
        conn.execute(
            "UPDATE ad_control_rule_group SET account_group_id='',account_ids_json='[]' "
            "WHERE group_id=?",
            (active_id,),
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "no resolved account IDs"):
            self.migrate()

    def test_pre_transaction_drift_blocks_all_owner_updates(self):
        first_id = sorted(GROUPS)[0]

        def drift(db_path):
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "UPDATE ad_control_rule_group SET name='drifted' WHERE group_id=?",
                (first_id,),
            )
            conn.commit()
            conn.close()

        with self.assertRaisesRegex(RuntimeError, "drifted before owner transaction"):
            migration.migrate_database(
                self.db, self.app, "892fd2e8", "codex", GROUPS,
                ensure_schema=self.fake_ensure,
                before_owner_transaction=drift,
            )
        conn = sqlite3.connect(str(self.db))
        owners = {
            row[0] for row in conn.execute(
                "SELECT owner_user_id FROM ad_control_rule_group"
            )
        }
        conn.close()
        self.assertEqual({"codex"}, owners)

    def test_mid_transaction_failure_rolls_back_all_owner_updates(self):
        self.fake_ensure(self.app, self.db)
        failing_id = sorted(GROUPS)[1]
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "CREATE TRIGGER fail_second_owner BEFORE UPDATE OF owner_user_id "
            "ON ad_control_rule_group WHEN OLD.group_id='%s' "
            "BEGIN SELECT RAISE(ABORT,'injected owner failure'); END" % failing_id
        )
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected owner failure"):
            self.migrate()
        conn = sqlite3.connect(str(self.db))
        owners = {
            row[0] for row in conn.execute(
                "SELECT owner_user_id FROM ad_control_rule_group"
            )
        }
        conn.close()
        self.assertEqual({"codex"}, owners)

    def test_trigger_side_effect_is_detected_before_commit_and_rolled_back(self):
        self.fake_ensure(self.app, self.db)
        first_id = sorted(GROUPS)[0]
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "CREATE TRIGGER mutate_name_after_owner AFTER UPDATE OF owner_user_id "
            "ON ad_control_rule_group WHEN OLD.group_id='%s' "
            "BEGIN UPDATE ad_control_rule_group SET name='trigger-drift' "
            "WHERE group_id=OLD.group_id; END" % first_id
        )
        original_name = conn.execute(
            "SELECT name FROM ad_control_rule_group WHERE group_id=?", (first_id,)
        ).fetchone()[0]
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(RuntimeError, "drifted before owner transaction"):
            self.migrate()
        conn = sqlite3.connect(str(self.db))
        row = conn.execute(
            "SELECT owner_user_id,name FROM ad_control_rule_group WHERE group_id=?",
            (first_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(("codex", original_name), row)

    def test_parameter_and_missing_file_gates(self):
        with self.assertRaisesRegex(RuntimeError, "exactly three"):
            migration.parse_expected_group_states(["one:p:1:0:0"])
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            migration.parse_expected_group_states([
                "one:p:1:0:0", "one:p:0:0:1", "three:p:0:0:1"
            ])
        with self.assertRaisesRegex(RuntimeError, "absolute"):
            migration.require_absolute_file("relative.sqlite3", "--db")
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            migration.require_absolute_file(self.root / "missing.sqlite3", "--db")

        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        arguments = [
            "--db", str(self.db), "--app", str(self.app),
            "--owner", "892fd2e9", "--expected-created-by", "codex",
        ]
        for group_id, state in GROUPS.items():
            arguments.extend([
                "--expected-group-state",
                "%s:%s:%s:%s:%s" % ((group_id,) + state),
            ])
        self.assertEqual(1, migration.main(arguments))
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

        wrong_state = list(arguments)
        wrong_state[-1] = wrong_state[-1].replace(":0:0:1", ":1:0:1")
        wrong_state[wrong_state.index("892fd2e9")] = "892fd2e8"
        self.assertEqual(1, migration.main(wrong_state))
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
