"""Real SQLite disposal, crash recovery and per-source isolation fixtures."""
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from features.pipeline_dispatch.core import DispatchStore
from features.pipeline_dispatch.adapters import ClosingConnection
from features.pipeline_dispatch.canonical_holds import SCHEMA as HOLD_SCHEMA
from scripts import pipeline_backlog_dispose as op


class BacklogDisposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = dt.datetime(2026, 11, 3, 4, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(self.root / 'central.sqlite', clock=lambda: self.now)
        self.config = {'db_path': self.store.path, 'paths': {}, 'drivers': [], 'preparation_policy': {}}
        for channel, family in op.KINDS:
            dbkey, table, pk, _, _ = op.KINDS[(channel, family)]
            path = self.root / (dbkey + '.sqlite'); self.config['paths'][dbkey] = str(path)
            self.config['drivers'].append({'channel': channel, 'families': [family], 'control_only': channel == 'x' and family == 'auto'})
            with sqlite3.connect(path, factory=ClosingConnection) as c:
                c.execute(HOLD_SCHEMA)
                c.execute('CREATE TABLE ' + table + '(' + pk + ''' TEXT PRIMARY KEY,
                    status TEXT,account_id TEXT,page_id TEXT,channel_id TEXT,
                    scheduled_at_utc TEXT,created_at TEXT,updated_at TEXT,finished_at_utc TEXT DEFAULT '',
                    material_id TEXT DEFAULT '',content_id TEXT DEFAULT '',template_id INTEGER DEFAULT 1,
                    template_version INTEGER DEFAULT 1,run_id INTEGER DEFAULT 1,execution_queue_id INTEGER,
                    claim_worker TEXT DEFAULT '',claim_token TEXT DEFAULT '',lease_expires_at_utc TEXT DEFAULT '',
                    publish_attempt_count INTEGER DEFAULT 0,unknown_outcome INTEGER DEFAULT 0,publish_id TEXT DEFAULT '',
                    prepared_json TEXT DEFAULT '{}',prepare_state TEXT DEFAULT 'ready',link_state TEXT DEFAULT 'ready',
                    prepared_media_url TEXT DEFAULT '',prepared_sha256 TEXT DEFAULT '',prepared_size_bytes INTEGER DEFAULT 0,
                    preflight_sha256 TEXT DEFAULT '',preflight_size INTEGER DEFAULT 0,preflight_duration REAL DEFAULT 0,
                    media_validation_mode TEXT DEFAULT 'deferred',random_template_enabled INTEGER DEFAULT 0,
                    owner_user_id TEXT DEFAULT 'u',owner_tenant_key TEXT DEFAULT 't',source_url TEXT DEFAULT '',
                    workflow TEXT DEFAULT 'legacy',privacy_status TEXT DEFAULT 'public',publish_at TEXT DEFAULT '')''')
        with sqlite3.connect(self.config['paths']['x'], factory=ClosingConnection) as c:
            c.execute('''CREATE TABLE x_post_publish_log(id INTEGER PRIMARY KEY,queue_id TEXT,status TEXT,
                attempt_count INTEGER DEFAULT 0,x_post_id TEXT DEFAULT '',unknown_outcome INTEGER DEFAULT 0,
                started_at TEXT DEFAULT '',published_at TEXT DEFAULT '',updated_at TEXT DEFAULT '')''')
        self.items = []

    def native(self, kind, ident):
        db, table, pk, _, _ = op.KINDS[kind]
        with contextlib.closing(op.connection(self.config['paths'][db])) as c:
            return dict(c.execute('SELECT * FROM ' + table + ' WHERE ' + pk + '=?', (str(ident),)).fetchone())

    def add(self, channel='tt', family='auto', ident=1, scheduled='2026-11-02T08:00:00+00:00', status=None, stage=None):
        kind = channel, family; db, table, pk, allowed, _ = op.KINDS[kind]
        state = status or ('pending' if family == 'auto' else 'queued')
        key = '%s:%s:%s' % (channel, family, ident)
        with sqlite3.connect(self.config['paths'][db], factory=ClosingConnection) as c:
            c.execute('INSERT INTO ' + table + '(' + pk + ',status,account_id,page_id,channel_id,scheduled_at_utc,created_at,updated_at,material_id,content_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (str(ident), state, 'account-' + str(ident), 'page-' + str(ident), 'channel-' + str(ident), scheduled,
                       '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', 'material-' + str(ident), 'content-' + str(ident)))
            c.execute('INSERT INTO pipeline_canonical_hold VALUES(?,?,?)', (key, '2026-11-02T12:00:00Z', 'cutover_overdue'))
        adapter = op.registry(self.config).adapters[kind]
        with adapter.connect() as c:
            r = c.execute('SELECT * FROM ' + table + ' WHERE ' + pk + '=?', (str(ident),)).fetchone()
            offer = adapter.project(adapter.enrich(c, r), now=self.now)
        if stage:
            offer['stage'] = stage
        job = self.store.offer(offer)
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET status='backlog_paused',hold_reason='cutover_overdue' WHERE id=?", (job['id'],))
        self.items.append((kind, str(ident), key))
        return job['id']

    def scope(self):
        items = []
        with contextlib.closing(op.connection(self.store.path)) as c:
            for kind, ident, key in self.items:
                db = op.KINDS[kind][0]
                with contextlib.closing(op.native_connection(self.config, db)) as n:
                    probe = {'channel': kind[0], 'family': kind[1], 'source_id': ident, 'source_key': key}
                    np = op.native_snapshot(n, probe); raw = np['source']
                    a = op.registry(self.config).adapters[kind]; offer = a.project(a.enrich(n, raw), now=self.now)
                cp = op.central_snapshot(c, key); schedule = offer['cutover_due_at'] or offer['scheduled_at']
                items.append(dict(probe, native_status=raw['status'], scheduled_at=schedule,
                                  scheduled_day_bjt=op.day(schedule), native_row_sha256=op.sha(raw),
                                  native_hold=np['hold'][0] if np['hold'] else None,
                                  jobs=[dict(j, row_sha256=op.sha(j)) for j in cp['jobs'] if j['status'] == 'backlog_paused']))
        value = {'read_only': True, 'day_basis': 'original_publish_schedule_Asia/Shanghai', 'target_date': '2026-11-02', 'items': items}
        value['snapshot_sha256'] = op.without_hash(value, 'snapshot_sha256')
        return value

    def plan(self):
        scope = self.scope()
        return op.make_plan(self.config, scope, scope['snapshot_sha256'], self.now)

    def apply(self, plan, **kwargs):
        kwargs.setdefault('planner_guard', lambda: True)
        return op.apply_plan(self.config, plan, plan['plan_sha256'], self.root / 'backups', 'fixture-operator', clock=lambda: self.now, **kwargs)

    def job(self, ident):
        with contextlib.closing(op.connection(self.store.path)) as c:
            return dict(c.execute('SELECT * FROM pipeline_job WHERE id=?', (ident,)).fetchone())

    def test_resume_uses_original_schedule_and_native_before_central_admission(self):
        ident = self.add(); original = self.native(('tt', 'auto'), 1); plan = self.plan()
        self.assertEqual(plan['blocked'], [])
        self.assertEqual(plan['items'][0]['action'], 'resume')
        observed = []
        def after_native(phase, item):
            self.assertEqual(phase, 'after_native_commit')
            with contextlib.closing(op.connection(self.config['paths']['tt_auto'])) as n:
                self.assertEqual(n.execute('SELECT action,state FROM pipeline_backlog_disposition').fetchone()[:], ('resume', 'committed'))
                self.assertEqual(n.execute('SELECT COUNT(*) FROM pipeline_canonical_hold').fetchone()[0], 0)
            self.assertEqual(self.job(ident)['status'], 'backlog_paused')
            observed.append(True)
        report = self.apply(plan, crash_hook=after_native)
        self.assertEqual(report['items'][0]['status'], 'committed')
        self.assertEqual(observed, [True])
        self.assertEqual(self.native(('tt', 'auto'), 1), original)
        self.assertEqual(self.job(ident)['status'], 'queued')
        self.assertEqual(self.job(ident)['scheduled_at'], plan['items'][0]['paused_jobs'][0]['scheduled_at'])

    def test_archive_all_native_families_preserves_holds_material_and_artifacts(self):
        originals = {}
        for number, kind in enumerate(op.KINDS, 1):
            self.add(*kind, ident=number, scheduled='2026-11-01T08:00:00+00:00')
            db, table, pk, _, _ = op.KINDS[kind]
            with sqlite3.connect(self.config['paths'][db], factory=ClosingConnection) as c:
                c.execute('UPDATE ' + table + ' SET prepared_json=? WHERE ' + pk + '=?',
                          (json.dumps({'artifact': {'sha256': 'a' * 64, 'size': 12345}, 'recipe_sha256': 'b' * 64}), str(number)))
            originals[kind] = self.native(kind, number)
        with sqlite3.connect(self.config['paths']['tt_auto'], factory=ClosingConnection) as c:
            c.execute('CREATE TABLE tt_auto_material_ledger(task_id TEXT, material_id TEXT, consumed INTEGER)')
            c.execute("INSERT INTO tt_auto_material_ledger VALUES('3','reserved-material',1)")
        plan = self.plan(); self.assertEqual(plan['blocked'], [])
        report = self.apply(plan)
        self.assertTrue(all(x['status'] == 'committed' for x in report['items']), report)
        for number, kind in enumerate(op.KINDS, 1):
            current = self.native(kind, number)
            self.assertEqual(current['status'], op.KINDS[kind][4])
            for k, v in originals[kind].items():
                if k not in ('status', 'updated_at', 'finished_at_utc'):
                    self.assertEqual(current[k], v)
            with contextlib.closing(op.connection(self.config['paths'][op.KINDS[kind][0]])) as n:
                self.assertEqual(n.execute('SELECT COUNT(*) FROM pipeline_canonical_hold').fetchone()[0], 1)
        with contextlib.closing(op.connection(self.store.path)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_job WHERE status='archived'").fetchone()[0], 5)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pipeline_job WHERE status IN ('completed','failed','published')").fetchone()[0], 0)
        with contextlib.closing(op.connection(self.config['paths']['tt_auto'])) as c:
            self.assertEqual(c.execute('SELECT * FROM tt_auto_material_ledger').fetchone()[:], ('3', 'reserved-material', 1))

    def test_x_auto_quiescence_required_before_backup_and_recovery_but_not_completed_replay(self):
        ident = self.add('x', 'auto'); plan = self.plan()
        def unavailable():
            raise op.DisposalRejected('x_auto_planner_not_quiescent')
        with self.assertRaisesRegex(op.DisposalRejected, 'x_auto_planner_not_quiescent'):
            self.apply(plan, planner_guard=unavailable)
        self.assertFalse((self.root / 'backups').exists())
        self.assertEqual(self.job(ident)['status'], 'backlog_paused')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.apply(plan, crash_hook=lambda *args: (_ for _ in ()).throw(RuntimeError('interrupted')))
        with self.assertRaisesRegex(op.DisposalRejected, 'x_auto_planner_not_quiescent'):
            self.apply(plan, planner_guard=unavailable)
        report = self.apply(plan)
        self.assertEqual(report['items'][0]['status'], 'committed', report)
        self.assertEqual(self.job(ident)['status'], 'queued')
        self.assertTrue(self.job(ident)['hold_reason'])  # Planner-only; no node claim.
        replay = self.apply(plan, planner_guard=unavailable)
        self.assertEqual(replay['items'][0]['status'], 'already_committed')

    def test_planner_probe_rejects_active_missing_or_live_pid(self):
        base = [dict(Id=u, LoadState='loaded', ActiveState='inactive', MainPID='0') for u in op.PLANNER_UNITS]
        def output(states):
            return '\n\n'.join('\n'.join(k + '=' + v for k, v in s.items()) for s in states)
        with patch.object(op.subprocess, 'check_output', return_value=output(base)):
            self.assertEqual(len(op.planner_quiescent()), len(op.PLANNER_UNITS))
        for field, value in [('ActiveState', 'active'), ('MainPID', '123'), ('LoadState', 'not-found')]:
            with self.subTest(field=field):
                changed = [dict(s) for s in base]; changed[0][field] = value
                with patch.object(op.subprocess, 'check_output', return_value=output(changed)):
                    with self.assertRaisesRegex(op.DisposalRejected, 'x_auto_planner_not_quiescent'):
                        op.planner_quiescent()
        with patch.object(op.subprocess, 'check_output', return_value=output(base[:-1])):
            with self.assertRaisesRegex(op.DisposalRejected, 'x_auto_planner_inventory_incomplete'):
                op.planner_quiescent()

    def test_related_legacy_schema_difference_blocks_only_affected_source(self):
        self.add('x', 'queue', ident=1); self.add('tt', 'auto', ident=2)
        scope = self.scope()
        with sqlite3.connect(self.config['paths']['x'], factory=ClosingConnection) as c:
            c.execute('CREATE TABLE pipeline_x_artifact(unexpected_old_key TEXT)')
            c.execute("INSERT INTO pipeline_x_artifact VALUES('must-not-ignore')")
        result = op.make_plan(self.config, scope, scope['snapshot_sha256'], self.now)
        self.assertEqual([i['source_key'] for i in result['items']], ['tt:auto:2'])
        self.assertEqual(result['blocked'][0]['source_key'], 'x:queue:1')
        self.assertEqual(result['blocked'][0]['code'], 'native_schema_or_database_conflict')

    def test_partial_central_claim_without_history_blocks_plan(self):
        ident = self.add()
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET claim_hash='partial-claim' WHERE id=?", (ident,))
        plan = self.plan()
        self.assertEqual(plan['items'], [])
        self.assertEqual(plan['blocked'][0]['code'], 'central_execution_evidence')

    def test_crash_after_native_commit_reentry_does_not_repeat_native_action(self):
        ident = self.add(); plan = self.plan()
        def crash(phase, item):
            raise RuntimeError('fixture_power_loss')
        with self.assertRaisesRegex(RuntimeError, 'fixture_power_loss'):
            self.apply(plan, crash_hook=crash)
        self.assertEqual(self.job(ident)['status'], 'backlog_paused')
        native_path = self.config['paths']['tt_auto']
        before = op.file_sha(native_path)
        report = self.apply(plan)
        self.assertEqual(report['items'][0]['status'], 'committed')
        self.assertEqual(op.file_sha(native_path), before)
        with contextlib.closing(op.connection(native_path)) as n:
            self.assertEqual(n.execute('SELECT COUNT(*) FROM pipeline_backlog_apply_journal').fetchone()[0], 1)
        # Later normal work is not re-executed or reset by rerunning this plan.
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET status='running',generation=1 WHERE id=?", (ident,))
        again = self.apply(plan)
        self.assertEqual(again['items'][0]['status'], 'already_committed')
        self.assertEqual(self.job(ident)['status'], 'running')

    def test_native_postcommit_drift_keeps_central_paused(self):
        ident = self.add(); plan = self.plan()
        with self.assertRaises(RuntimeError):
            self.apply(plan, crash_hook=lambda *args: (_ for _ in ()).throw(RuntimeError('crash')))
        with sqlite3.connect(self.config['paths']['tt_auto'], factory=ClosingConnection) as n:
            n.execute("UPDATE tt_auto_task SET material_id='changed' WHERE id='1'")
        result = self.apply(plan)
        self.assertEqual(result['items'][0]['code'], 'native_postcommit_drift')
        self.assertEqual(self.job(ident)['status'], 'backlog_paused')

    def test_single_source_drift_isolated_while_other_source_and_live_work_unchanged(self):
        first = self.add(ident=1); second = self.add(ident=2); outside = self.add(ident=3)
        self.items.pop()
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET status='running',generation=1 WHERE id=?", (outside,))
        before = self.job(outside); plan = self.plan()
        with sqlite3.connect(self.config['paths']['tt_auto'], factory=ClosingConnection) as n:
            n.execute("UPDATE tt_auto_task SET material_id='changed' WHERE id='1'")
        result = self.apply(plan)
        self.assertEqual([x['status'] for x in result['items']], ['blocked', 'committed'])
        self.assertEqual(self.job(first)['status'], 'backlog_paused')
        self.assertEqual(self.job(second)['status'], 'queued')
        self.assertEqual(self.job(outside), before)

    def test_only_two_observational_fields_may_drift(self):
        first = self.add(ident=1); second = self.add(ident=2); plan = self.plan()
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_job SET updated_at='2026-11-03T04:01:00Z',source_status='pending' WHERE id=?", (first,))
            c.execute("UPDATE pipeline_job SET owner_tenant_key='other-tenant' WHERE id=?", (second,))
        result = self.apply(plan)
        self.assertEqual([x['status'] for x in result['items']], ['committed', 'blocked'])
        self.assertEqual(result['items'][1]['code'], 'central_plan_drift')

    def test_publish_unknown_active_claim_and_execution_history_fail_closed(self):
        mutations = ["UPDATE tt_auto_task SET publish_attempt_count=1", "UPDATE tt_auto_task SET unknown_outcome=1",
                     "UPDATE tt_auto_task SET lease_expires_at_utc='2099-01-01T00:00:00Z'", "UPDATE tt_auto_task SET claim_token='held'",
                     "UPDATE tt_auto_task SET publish_id='existing-platform-id'"]
        for index, sql in enumerate(mutations, 10):
            with self.subTest(sql=sql):
                self.items.clear(); ident = self.add(ident=index)
                db = self.config['paths']['tt_auto']
                with sqlite3.connect(db, factory=ClosingConnection) as n:
                    n.execute(sql + ' WHERE id=?', (str(index),))
                plan = self.plan()
                self.assertEqual(plan['items'], [])
                self.assertEqual(len(plan['blocked']), 1)
                self.assertEqual(self.job(ident)['status'], 'backlog_paused')

    def test_bad_hash_and_backup_tamper_block_before_admission(self):
        ident = self.add(); plan = self.plan()
        with self.assertRaisesRegex(op.DisposalRejected, 'plan_hash_mismatch'):
            op.apply_plan(self.config, plan, 'f' * 64, self.root / 'backups', 'fixture')
        self.assertFalse((self.root / 'backups').exists())
        manifest = op.backup_databases(plan, self.root / 'backups')
        with Path(manifest['databases'][0]['path']).open('ab') as f:
            f.write(b'tampered')
        with self.assertRaisesRegex(op.DisposalRejected, 'backup_hash_mismatch'):
            self.apply(plan)
        self.assertEqual(self.job(ident)['status'], 'backlog_paused')

    def test_stage_mismatch_is_reported_not_recreated(self):
        self.add(ident=1, stage='publish')
        plan = self.plan()
        self.assertEqual(plan['items'], [])
        self.assertEqual(plan['blocked'][0]['code'], 'requested_stage_missing')

    def test_published_ghost_archives_only_prepare_and_never_touches_native(self):
        ident = self.add('x', 'queue', ident=41, scheduled='2026-11-01T08:00:00+00:00', stage='prepare')
        path = self.config['paths']['x']
        with sqlite3.connect(path, factory=ClosingConnection) as n:
            n.execute("UPDATE x_post_queue SET status='published' WHERE id='41'")
            n.execute("DELETE FROM pipeline_canonical_hold WHERE source_key='x:queue:41'")
            n.execute("INSERT INTO x_post_publish_log(id,queue_id,status,attempt_count,x_post_id,published_at,updated_at) VALUES(1,'41','published',1,'post-original','2026-11-02T10:00:00Z','2026-11-02T10:00:00Z')")
        # A separate existing publication is the authority, never the paused
        # preparation stage. Use the ordinary collector to create that fact.
        adapter = op.registry(self.config).adapters[('x', 'queue')]
        with adapter.connect() as n:
            row = n.execute("SELECT * FROM x_post_queue WHERE id='41'").fetchone()
            offer = adapter.project(adapter.enrich(n, row), now=self.now)
        offer['stage'] = 'publish'; offer['status'] = 'published'; offer['finished_at'] = '2026-11-02T10:00:00Z'
        publish = self.store.offer(offer)
        self.store.record_publication(publish['id'], {'source_key': 'x:queue:41', 'publish_id': 'post-original',
                                     'published_at': '2026-11-02T10:00:00Z'}, canonical_verified=True)
        plan = self.plan(); self.assertEqual(plan['blocked'], [])
        before = op.file_sha(path); pubbefore = self.job(publish['id'])
        result = self.apply(plan)
        self.assertEqual(result['items'][0]['status'], 'committed', result)
        self.assertFalse(result['items'][0]['native_modified'])
        self.assertEqual(self.job(ident)['status'], 'archived')
        self.assertEqual(self.job(publish['id']), pubbefore)
        self.assertEqual(op.file_sha(path), before)


if __name__ == '__main__':
    unittest.main()
