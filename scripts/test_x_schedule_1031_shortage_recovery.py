"""Exact dependency barrier, read-only waiting, and atomic once-only admission."""
import contextlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import recover_x_schedule_1031_shortage as recovery
from scripts import recover_x_zero_write_schedules as first
from scripts import recover_x_drama_episode_selection as episode
from scripts import test_x_zero_write_schedule_recovery as base
from scripts.test_x_post_multi_schedule_store import base_candidate


class ShortageRecoveryTests(unittest.TestCase):
    create = base.RecoveryTests.create
    row = base.RecoveryTests.row
    update = base.RecoveryTests.update

    def setUp(self):
        base.RecoveryTests.setUp(self)
        self.central = Path(self.temp.name) / 'central.sqlite3'
        self.create(1031, '2026-11-03', 'x_accounts_unavailable')
        self.update("UPDATE x_post_schedule_run SET source_type='drama',material_pool_snapshot_max_id=NULL WHERE id=1031")
        saved = first.plan(self.path, [1031], ['2026-11-03'])
        first.apply(self.path, saved, saved['plan_sha256'], Path(self.temp.name) / 'first', 'first', 'original')
        self.update(episode.SCHEMA)
        self.update('INSERT INTO ' + episode.TABLE + ' VALUES(?,?,?,?,?,?,?,?)',
            (1031, recovery.fingerprint(recovery.identity(self.row(1031))), 'second-plan', 'other-actor',
             'second exact recovery', '2026-11-03T12:45:17Z', 'backup', '{"original_failure":"preserved"}'))
        self.update("UPDATE x_post_schedule_run SET status='failed_preflight',error_code=?,error_message=?,"
                    "finished_at=?,updated_at=? WHERE id=1031",
                    (recovery.ERROR, recovery.MESSAGE, recovery.ANCHOR['failed_at'], recovery.ANCHOR['failed_at']))
        self.create(1029, '2026-11-02', '', 'queued')
        self.update("UPDATE x_post_schedule_run SET source_type='drama' WHERE id=1029")
        with contextlib.closing(sqlite3.connect(self.path)) as n:
            n.execute('CREATE TABLE pipeline_x_operation(operation_id TEXT,source_key TEXT,generation INTEGER,operation TEXT,request_sha256 TEXT)')
            for qid in recovery.DEPENDENCIES:
                n.execute("INSERT INTO x_post_drama_pool(id,content_id,status,language,created_at,updated_at) "
                          "VALUES(?,?,'completed','en','before','before')", (qid, 'drama-' + str(qid)))
                payload = base_candidate(qid - 5631, 'test', qid, 'drama-' + str(qid))
                payload.update(id=qid, schedule_run_id=1029, run_date='2026-11-02',
                    idempotency_key='dependency-' + str(qid), material_key=str(qid), status='published',
                    source_type='drama', drama_pool_item_id=qid, drama_replay_generation=1,
                    episode_key='drama-' + str(qid) + ':1', episode_number=1,
                    random_template_enabled=1, duration_policy_version=2,
                    created_at='2026-11-03T12:47:35Z', updated_at='settled')
                n.execute('INSERT INTO x_post_queue(' + ','.join(payload) + ') VALUES(' +
                          ','.join('?' for _ in payload) + ')', list(payload.values()))
                n.execute("INSERT INTO x_post_publish_log(queue_id,account_id,status,attempt_count,x_post_id,published_at,created_at,updated_at) "
                          "VALUES(?,?,'published',1,?,'settled','created','settled')", (qid, qid - 5631, 'post-' + str(qid)))
            n.execute("INSERT INTO x_post_drama_pool(content_id,status,language,free_episode_count,created_at,updated_at) "
                      "VALUES('available','pending','en',5,'before','before')")
            n.commit()
        with contextlib.closing(sqlite3.connect(self.central)) as c:
            c.executescript('''CREATE TABLE pipeline_job(id INTEGER PRIMARY KEY,source_key TEXT,stage TEXT,status TEXT,
              generation INTEGER,hold_reason TEXT,lease_expires_at TEXT,external_intent INTEGER,error_code TEXT,updated_at TEXT);
              CREATE TABLE pipeline_resource(resource_key TEXT,job_id INTEGER,generation INTEGER,quarantined INTEGER);
              CREATE TABLE pipeline_legacy_resource(resource_key TEXT,job_id INTEGER);
              CREATE TABLE pipeline_publication(source_key TEXT,job_id INTEGER,publish_id TEXT,published_at TEXT);
              CREATE TABLE pipeline_operation(operation_id TEXT,job_id INTEGER,generation INTEGER,operation TEXT,request_sha256 TEXT);''')
            for qid in recovery.DEPENDENCIES:
                c.execute("INSERT INTO pipeline_job VALUES(?,?,'publish','published',1,'','',1,'','settled')", (qid, 'x:queue:' + str(qid)))
                c.execute('INSERT INTO pipeline_publication VALUES(?,?,?,?)', ('x:queue:' + str(qid), qid, 'post-' + str(qid), 'settled'))
            c.commit()
        with contextlib.closing(recovery.read_connection(self.path)) as n:
            anchor = dict(recovery.ANCHOR, run_sha256=recovery.fingerprint(self.row(1031)),
                identity_sha256=recovery.fingerprint(recovery.identity(self.row(1031))),
                audits={t: recovery.fingerprint(recovery.audit(n, t, 1031)[0]) for t in (recovery.FIRST, recovery.SECOND)},
                dependencies_sha256=recovery.fingerprint(recovery.rows(n, 'SELECT ' + ','.join(recovery.QUEUE_IDENTITY) +
                    ' FROM x_post_queue WHERE schedule_run_id=1029 ORDER BY id')))
        patch = mock.patch.object(recovery, 'ANCHOR', anchor); patch.start(); self.addCleanup(patch.stop)
        self.deployed = Path(self.temp.name) / 'deployed'
        blobs = {}
        for name in recovery.CODE_FILES:
            data = (Path(__file__).resolve().parents[1] / name).read_bytes()
            path = self.deployed / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            blobs[name] = data
        git = mock.patch.object(recovery.subprocess, 'run', side_effect=lambda args, **kw:
            SimpleNamespace(returncode=0, stdout=blobs[args[-1].split(':', 1)[1]]))
        git.start(); self.addCleanup(git.stop)

    def plan(self):
        return recovery.plan(self.path, self.central, 'a' * 40,
                             self.deployed / recovery.CODE_FILES[0], self.temp.name)

    def apply(self, saved):
        return recovery.apply(saved, saved['plan_sha256'], Path(self.temp.name) / 'third', 'operator', 'one original slot admission')

    def central_update(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(self.central)) as c:
            c.execute(sql, args); c.commit()

    def db_dump(self, path):
        with contextlib.closing(recovery.read_connection(path)) as n: return '\n'.join(n.iterdump())

    def test_ready_once_retains_identity_history_and_all_other_queues(self):
        before = self.row(1031); previous = self.row(1029)
        original_native = self.db_dump(self.path); original_central = self.db_dump(self.central)
        saved = self.plan()
        self.assertEqual(saved['snapshot']['state'], 'ready')
        self.assertEqual(len(saved['snapshot']['dependencies']), 19)
        self.assertEqual(len(saved['snapshot']['eligibility']['account_reasons']), 2)
        self.assertEqual(self.db_dump(self.path), original_native)
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            result = self.apply(saved)
        self.assertFalse(result['platform_called']); self.assertEqual(result['queues_created'], 0)
        self.assertEqual(recovery.identity(self.row(1031)), recovery.identity(before))
        self.assertEqual(self.row(1031)['status'], 'claimed')
        self.assertEqual(self.row(1029), previous)
        self.assertEqual(self.db_dump(self.central), original_central)
        with contextlib.closing(recovery.read_connection(self.path)) as n:
            recorded = recovery.audit(n, recovery.TABLE, 1031)[0]
            proof = json.loads(recorded['evidence_json'])
            self.assertEqual(proof['snapshot']['original']['previous_run'], before)
            self.assertEqual(n.execute('SELECT count(*) FROM x_post_queue').fetchone()[0], 19)
            self.assertIn(1031, base.authorized_run_ids(n))
            for table, expected in recovery.ANCHOR['audits'].items():
                self.assertEqual(recovery.fingerprint(recovery.audit(n, table, 1031)[0]), expected)
        self.update("UPDATE x_post_schedule_run SET status='failed_preflight',plan_attempted_at='later-attempt',error_code='later-failure' WHERE id=1031")
        progressed = self.row(1031)
        self.assertTrue(self.apply(saved)['already_applied'])
        self.assertEqual(self.row(1031), progressed)

    def test_any_of_19_unfinished_blocks_even_when_current_candidates_exist(self):
        for qid in recovery.DEPENDENCIES:
            self.update("UPDATE x_post_queue SET status='queued' WHERE id=?", (qid,))
            saved = self.plan()
            self.assertEqual(saved['snapshot']['pending_queue_ids'], [qid])
            self.assertIsNone(saved['snapshot']['eligibility'])
            with self.assertRaises(recovery.RecoveryRejected): self.apply(saved)
            self.assertEqual(self.row(1031)['status'], 'failed_preflight')
            self.update("UPDATE x_post_queue SET status='published' WHERE id=?", (qid,))

    def test_unknown_lease_resource_or_receipt_difference_keeps_waiting(self):
        changes = [
            (False, 'UPDATE x_post_publish_log SET unknown_outcome=1 WHERE queue_id=5633', 'UPDATE x_post_publish_log SET unknown_outcome=0 WHERE queue_id=5633'),
            (True, "UPDATE pipeline_job SET lease_expires_at='active' WHERE id=5633", "UPDATE pipeline_job SET lease_expires_at='' WHERE id=5633"),
            (True, "INSERT INTO pipeline_resource VALUES('x:2',5633,1,0)", 'DELETE FROM pipeline_resource'),
            (True, "UPDATE pipeline_publication SET publish_id='different' WHERE job_id=5633", "UPDATE pipeline_publication SET publish_id='post-5633' WHERE job_id=5633"),
            (True, "INSERT INTO pipeline_operation VALUES('op',5633,1,'x:GET:/2/media/upload','hash')", 'DELETE FROM pipeline_operation'),
        ]
        for central, change, undo in changes:
            write = self.central_update if central else self.update
            write(change)
            self.assertEqual(self.plan()['snapshot']['state'], 'waiting_dependencies')
            write(undo)

    def test_known_prepost_failure_is_terminal_but_post_intent_never_is(self):
        self.update("UPDATE x_post_queue SET status='failed' WHERE id=5633")
        self.update("UPDATE x_post_publish_log SET status='failed',x_post_id='',published_at='' WHERE queue_id=5633")
        self.central_update("UPDATE pipeline_job SET status='failed' WHERE id=5633")
        self.central_update('DELETE FROM pipeline_publication WHERE job_id=5633')
        self.assertEqual(self.plan()['snapshot']['state'], 'ready')
        self.update("INSERT INTO pipeline_x_operation VALUES('op','x:queue:5633',1,'x:POST:/2/tweets','hash')")
        self.central_update("INSERT INTO pipeline_operation VALUES('op',5633,1,'x:POST:/2/tweets','hash')")
        saved = self.plan()
        self.assertEqual(saved['snapshot']['dependencies'][0]['wait_reason'], 'dependency_failure_not_proven_prepost')

    def test_exact_failure_audit_dependency_identity_or_existing_queue_drift_rejected(self):
        for column, value in [('status', 'claimed'), ('updated_at', 'actor'), ('plan_attempted_at', 'attempt'),
                              ('unknown_count', 1), ('body_template', 'different')]:
            previous = self.row(1031)[column]
            self.update('UPDATE x_post_schedule_run SET ' + column + '=? WHERE id=1031', (value,))
            with self.assertRaises(recovery.RecoveryRejected): self.plan()
            self.update('UPDATE x_post_schedule_run SET ' + column + '=? WHERE id=1031', (previous,))
        self.update("UPDATE x_post_queue SET episode_number=99 WHERE id=5633")
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'dependency_frozen'): self.plan()
        self.update("UPDATE x_post_queue SET episode_number=1 WHERE id=5633")
        self.update("UPDATE x_post_queue SET schedule_run_id=1031,run_date='2026-11-03' WHERE id=5633")
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'original_slot_has_queue'): self.plan()
        self.update("UPDATE x_post_queue SET schedule_run_id=1029,run_date='2026-11-02' WHERE id=5633")
        self.update('UPDATE ' + recovery.SECOND + " SET reason='other actor changed evidence'")
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'previous_audit'): self.plan()

    def test_fresh_candidates_required_and_normal_blockers_retained(self):
        self.update("UPDATE x_post_drama_pool SET status='completed'")
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'no_fresh_candidates'): self.plan()
        self.update("UPDATE x_post_drama_pool SET status='pending'")
        self.update("UPDATE x_authorized_account SET status='disabled' WHERE id=2")
        saved = self.plan()
        candidates = saved['snapshot']['eligibility']['candidates']
        self.assertEqual([r['candidate_account_id'] for r in candidates], [3])
        self.assertEqual(saved['snapshot']['eligibility']['account_reasons'][0]['reason'], 'x_account_not_publishable')

    def test_cas_after_backup_and_transaction_rollback(self):
        saved = self.plan(); original = recovery.backup
        def changed(*args):
            target = original(*args)
            self.update("UPDATE x_post_schedule_run SET updated_at='external actor' WHERE id=1031")
            return target
        with mock.patch.object(recovery, 'backup', side_effect=changed), self.assertRaises(recovery.RecoveryRejected):
            self.apply(saved)
        self.update('UPDATE x_post_schedule_run SET updated_at=? WHERE id=1031', (recovery.ANCHOR['failed_at'],))
        self.update("CREATE TRIGGER abort_admission BEFORE UPDATE ON x_post_schedule_run WHEN NEW.id=1031 AND NEW.status='claimed' BEGIN SELECT RAISE(ABORT,'injected rollback'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected rollback'): self.apply(saved)
        self.assertEqual(self.row(1031)['status'], 'failed_preflight')
        with contextlib.closing(recovery.read_connection(self.path)) as n:
            self.assertEqual(recovery.audit(n, recovery.TABLE, 1031), [])

    def test_native_occupancy_or_central_restart_after_plan_refuses_apply(self):
        saved = self.plan()
        self.update("UPDATE x_post_drama_pool SET status='completed'")
        with self.assertRaises(recovery.RecoveryRejected): self.apply(saved)
        self.update("UPDATE x_post_drama_pool SET status='pending'")
        self.central_update("UPDATE pipeline_job SET status='running' WHERE id=5633")
        with self.assertRaises(recovery.RecoveryRejected): self.apply(saved)

    def test_final_central_read_drift_rolls_back_admission(self):
        saved = self.plan(); original = recovery.dependencies; calls = []
        def drift(n, c):
            calls.append(True)
            if len(calls) == 3:  # Precheck, transaction check, then before commit.
                self.central_update("UPDATE pipeline_job SET status='running' WHERE id=5633")
            return original(n, c)
        with mock.patch.object(recovery, 'dependencies', side_effect=drift), \
                self.assertRaisesRegex(recovery.RecoveryRejected, 'central_dependency_drift'):
            self.apply(saved)
        self.assertEqual(self.row(1031)['status'], 'failed_preflight')
        with contextlib.closing(recovery.read_connection(self.path)) as n:
            self.assertEqual(recovery.audit(n, recovery.TABLE, 1031), [])

    def test_other_slot_or_tampered_plan_cannot_use_this_admission(self):
        saved = self.plan()
        saved['run_id'] = 1029
        saved['plan_sha256'] = recovery.fingerprint({k: v for k, v in saved.items() if k != 'plan_sha256'})
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'plan_hash'): self.apply(saved)
        saved = self.plan(); saved['snapshot']['original']['previous_run']['body_template'] = 'changed'
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'plan_hash'): self.apply(saved)

    def test_append_only_including_insert_or_replace_and_deployed_code_drift(self):
        saved = self.plan(); self.apply(saved)
        for sql in ('UPDATE ' + recovery.TABLE + " SET reason='changed'", 'DELETE FROM ' + recovery.TABLE,
                    'INSERT OR REPLACE INTO ' + recovery.TABLE + ' SELECT * FROM ' + recovery.TABLE):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'append_only'): self.update(sql)
        path = self.deployed / recovery.CODE_FILES[1]
        path.write_bytes(path.read_bytes() + b'\n# drift')
        with self.assertRaisesRegex(recovery.RecoveryRejected, 'deployed_code_drift'): self.plan()

    def test_wait_is_readonly_bounded_and_drift_stops_without_admitting(self):
        self.update("UPDATE x_post_queue SET status='queued' WHERE id=5633")
        native_before, central_before = self.db_dump(self.path), self.db_dump(self.central)
        ticks = iter([0, 0, 2]); observations = []
        saved = recovery.wait_plan(self.plan, 1, 1, observations.append,
            monotonic=lambda: next(ticks), sleep=lambda seconds: None)
        self.assertEqual(saved['snapshot']['state'], 'waiting_dependencies')
        self.assertEqual(len(observations), 2)
        self.assertEqual(self.db_dump(self.path), native_before)
        self.assertEqual(self.db_dump(self.central), central_before)
        observations = []
        def actor(_seconds): self.update("UPDATE x_post_schedule_run SET updated_at='external actor' WHERE id=1031")
        with self.assertRaises(recovery.RecoveryRejected):
            recovery.wait_plan(self.plan, 60, 1, observations.append, monotonic=lambda: 0, sleep=actor)
        self.assertEqual(len(observations), 1)
        self.assertEqual(self.row(1031)['status'], 'failed_preflight')

    def test_wait_exits_at_ready_without_apply_or_publish(self):
        self.update("UPDATE x_post_queue SET status='queued' WHERE id=5633")
        def settled(_seconds): self.update("UPDATE x_post_queue SET status='published' WHERE id=5633")
        saved = recovery.wait_plan(self.plan, 60, 1, lambda _: None, monotonic=lambda: 0, sleep=settled)
        self.assertEqual(saved['snapshot']['state'], 'ready')
        self.assertEqual(self.row(1031)['status'], 'failed_preflight')


if __name__ == '__main__': unittest.main()
