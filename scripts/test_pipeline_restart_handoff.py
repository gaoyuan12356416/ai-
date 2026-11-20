import contextlib
import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from features.pipeline_dispatch import restart_handoff as handoff
from features.pipeline_dispatch.core import DispatchStore, digest, stamp
from scripts import pipeline_service_drain as drain


class RestartHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = dt.datetime.now(dt.timezone.utc)
        self.store = DispatchStore(self.root / 'dispatch.db', clock=lambda: self.now)
        self.store.register_node('cpu-main', ['prepare:fb'], {'backend': 'cpu'})
        self.store.worker_heartbeat('cpu-main', 'worker', 'prepare', pid=123,
                                    observation={'phase': 'render'})
        self.job = self.store.offer({'source_key': 'fb:auto:1', 'source_id': '1',
            'channel': 'fb', 'family': 'auto', 'stage': 'prepare',
            'payload': {'kind': 'fb_prepare'}, 'resource_keys': ['material:1']})
        with self.store.transaction() as c:
            c.execute("""UPDATE pipeline_job SET status='running',generation=1,
                node_id='cpu-main',worker_id='worker',claim_hash=?,lease_expires_at=?
                WHERE id=?""", (digest('claim'), stamp(self.now + dt.timedelta(seconds=120)), self.job['id']))
            c.execute('INSERT INTO pipeline_resource(resource_key,job_id,generation) VALUES(?,?,1)',
                      ('material:1', self.job['id']))

    def state(self, unit=handoff.COORDINATOR, restarting=True):
        with contextlib.closing(self.store.connect()) as c:
            return handoff.assess(c, unit, restarting=restarting, now=self.now)

    def change(self, **values):
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_job SET ' + ','.join(key + '=?' for key in values)
                      + ' WHERE id=?', (*values.values(), self.job['id']))

    def test_local_render_continues_only_during_coordinator_restart(self):
        initial = self.store.get_job(self.job['id'])
        self.assertEqual(len(self.state()['continuing_renders']), 1)
        self.assertFalse(self.state()['pending'])
        self.assertEqual(len(self.state(restarting=False)['pending']), 1)
        self.change(channel='x', source_id='9000000000001')
        for unit in ('x-post-automation.service', 'x-tag-post.service', 'drama-material-api.service'):
            self.assertEqual(len(self.state(unit)['pending']), 1)
        self.change(channel='fb', source_id='1')
        self.assertEqual(self.store.get_job(self.job['id']), initial)

    def test_publications_and_unknown_executors_always_wait(self):
        cases = [dict(stage='publish'), dict(status='legacy_running'),
                 dict(generation=0), dict(external_intent=1),
                 dict(payload_json='{}'), dict(payload_json='{'),
                 dict(payload_json=json.dumps({'kind': 'youtube_prepare'})),
                 dict(payload_json=json.dumps({'kind': 'future_renderer'}))]
        for changes in cases:
            with self.subTest(changes=changes):
                self.change(stage='prepare', status='running', generation=1,
                            external_intent=0, payload_json=json.dumps({'kind': 'fb_prepare'}))
                self.change(**changes)
                self.assertEqual(len(self.state()['pending']), 1)
                self.assertFalse(self.state()['continuing_renders'])

    def test_x_preflight_render_is_carried_but_its_checkpoint_phase_waits(self):
        self.change(channel='x', payload_json=json.dumps({'kind': 'x_preflight'}))
        self.assertEqual(len(self.state()['continuing_renders']), 1)
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_worker SET observation_json=?',
                      (json.dumps({'phase': 'probe'}),))
        self.assertEqual(len(self.state()['pending']), 1)

    def test_stale_missing_or_nonrender_observation_waits(self):
        for observation, age in [({'phase': 'download'}, 0), ({'phase': 'idle'}, 0),
                                  ({'phase': 'render'}, 31), ({}, 0)]:
            with self.subTest(observation=observation, age=age), self.store.transaction() as c:
                c.execute('UPDATE pipeline_worker SET observation_json=?,last_seen_at=?',
                          (json.dumps(observation), stamp(self.now-dt.timedelta(seconds=age))))
            self.assertEqual(len(self.state()['pending']), 1)
        with self.store.transaction() as c:
            c.execute('DELETE FROM pipeline_worker')
        self.assertEqual(len(self.state()['pending']), 1)

    def test_cloud_and_expiring_leases_are_not_carried(self):
        for lease in ('bad-time', stamp(self.now+dt.timedelta(seconds=89)),
                      stamp(self.now-dt.timedelta(seconds=1))):
            self.change(lease_expires_at=lease)
            self.assertEqual(len(self.state()['pending']), 1)
        self.change(lease_expires_at=stamp(self.now+dt.timedelta(seconds=120)))
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_node SET metrics_json='{}'")
        self.assertEqual(len(self.state()['pending']), 1)
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_node SET metrics_json=?', (json.dumps({'backend': 'cpu'}),))
            c.execute('INSERT INTO pipeline_cloud_intent VALUES(?,?,?,?)',
                      (self.job['id'], 1, 'cloud-action', stamp(self.now)))
        self.assertEqual(len(self.state()['pending']), 1)

    def test_recorded_external_operation_blocks_even_without_intent_flag(self):
        with self.store.transaction() as c:
            c.execute("""INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,
                operation,request_sha256,account_resource,created_at,updated_at)
                VALUES('op',?,1,'cpu-main','upload',?,'material:1',?,?)""",
                      (self.job['id'], 'a'*64, stamp(self.now), stamp(self.now)))
        self.assertEqual(len(self.state()['pending']), 1)

    def test_new_coordinator_renews_same_claim_before_admission_reopens(self):
        carried = self.state()['continuing_renders']
        ready = self.now + dt.timedelta(seconds=10)
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(handoff.resumed(c, carried, ready, now=ready)['waiting'], [self.job['id']])
        # Reconstruct the coordinator over its existing ledger, as a restart does.
        self.now = ready + dt.timedelta(seconds=1)
        replacement = DispatchStore(self.store.path, clock=lambda: self.now)
        claim = {'job_id': self.job['id'], 'generation': 1, 'node_id': 'cpu-main',
                 'worker_id': 'worker', 'claim_token': 'claim'}
        self.assertTrue(replacement.heartbeat(claim, phase='render')['accepted'])
        with contextlib.closing(replacement.connect()) as c:
            self.assertFalse(handoff.resumed(c, carried, ready, now=self.now)['waiting'])
            resource = dict(c.execute('SELECT * FROM pipeline_resource').fetchone())
        self.assertEqual(resource['job_id'], self.job['id'])
        self.assertEqual(resource['generation'], 1)
        self.assertEqual(replacement.get_job(self.job['id'])['worker_id'], 'worker')

    def test_identity_change_waits_and_terminal_unknown_retains_quarantine(self):
        carried = self.state()['continuing_renders']
        self.change(generation=2)
        with contextlib.closing(self.store.connect()) as c:
            self.assertTrue(handoff.resumed(c, carried, self.now, now=self.now)['waiting'])
        self.change(generation=1, status='unknown', external_intent=1)
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_resource SET quarantined=1')
        with contextlib.closing(self.store.connect()) as c:
            result = handoff.resumed(c, carried, self.now, now=self.now)
            self.assertEqual(result['attention'], [{'id': self.job['id'], 'status': 'unknown'}])
            self.assertEqual(c.execute('SELECT quarantined FROM pipeline_resource').fetchone()[0], 1)
        self.assertEqual(self.store.get_job(self.job['id'])['status'], 'unknown')

    def test_systemd_job_must_explicitly_be_restart(self):
        for kind, expected in [('restart', True), ('stop', False), ('start', False)]:
            output = '23 pipeline-coordinator.service %s running\n' % kind
            with patch.object(drain.subprocess, 'run', return_value=Mock(stdout=output)):
                self.assertEqual(drain.planned_restart(handoff.COORDINATOR), expected)
                self.assertFalse(drain.planned_restart('x-post-automation.service'))
        with patch.object(drain.subprocess, 'run', side_effect=subprocess.TimeoutExpired('systemctl', 3)):
            self.assertFalse(drain.planned_restart(handoff.COORDINATOR))

    def test_startup_does_not_release_hold_before_render_heartbeat(self):
        hold = self.root / (handoff.COORDINATOR + '.json')
        hold.write_text(json.dumps({'continuing_renders': self.state()['continuing_renders']}))
        response = Mock(); response.status = 200
        opened = Mock(); opened.__enter__ = Mock(return_value=response); opened.__exit__ = Mock(return_value=False)
        opener = Mock(); opener.open.return_value = opened
        def still_held(_):
            self.assertTrue(hold.exists())
        with patch.object(drain.urllib.request, 'build_opener', return_value=opener), \
             patch.object(drain.time, 'sleep', side_effect=still_held), \
             patch.object(handoff, 'resumed', side_effect=[{'waiting': [self.job['id']], 'attention': []},
                                                          {'waiting': [], 'attention': []}]):
            drain.release(handoff.COORDINATOR, self.root, self.store.path)
        self.assertFalse(hold.exists())

    def test_failed_startup_keeps_admission_hold(self):
        hold = self.root / (handoff.COORDINATOR + '.json')
        hold.write_text('{}')
        opener = Mock(); opener.open.side_effect = OSError('not ready')
        with patch.object(drain.urllib.request, 'build_opener', return_value=opener), \
             patch.object(drain.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'drain_preserved'):
                drain.release(handoff.COORDINATOR, self.root, self.store.path)
        self.assertTrue(hold.exists())


if __name__ == '__main__':
    unittest.main()
