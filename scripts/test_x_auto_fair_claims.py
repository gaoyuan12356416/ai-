"""Exercise fair claims, receipt rotation and existing gates in real SQLite."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_auto_posts.core import XPostAutoStore
from scripts import test_x_auto_post_store as fixtures


class FairClaimTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AutoPostStoreTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store = self.f.store
        self.template = self.f.create_template()
        self.sequence = 0

    def task(self, observation=False, *, status='retry_wait', due_offset=-60):
        self.sequence += 1
        task = self.f.create_task(self.f.create_run(self.template, 'fair-' + str(self.sequence)))
        if observation:
            task = self.store.transition_task(task.id, status, expected_statuses={'pending'}, updates={
                'claim_phase': 'reconcile', 'execution_run_id': 1000 + task.id,
                'execution_queue_id': 2000 + task.id,
                'next_attempt_at_utc': self.f.clock() + timedelta(seconds=due_offset),
            })
        return task

    def claim(self, **kwargs):
        return self.store.claim_next_executable_task(worker_id='fair-test', lease_seconds=120,
                                                     publish_delegated=True, **kwargs)

    def finish(self, claim):
        if claim.claim_phase == 'reconcile':
            status = 'retry_wait'
            updates = {'claim_phase': 'reconcile',
                       'next_attempt_at_utc': self.f.clock() + timedelta(seconds=30)}
        else:
            status, updates = 'no_candidate', {'claim_phase': ''}
        updates.update(claim_token='', claim_worker='', lease_expires_at_utc='')
        self.store.transition_task(claim.task.id, status, expected_statuses={claim.task.status},
                                   claim_token=claim.reveal_claim_token(), updates=updates)

    def test_more_than_a_cycle_of_receipts_cannot_starve_selection_or_each_other(self):
        observations = [self.task(True).id for _ in range(25)]
        progress = [self.task().id for _ in range(25)]
        seen_old, seen_new = [], []
        for cycle in range(3):
            phases = []
            for _ in range(20 if cycle < 2 else 10):
                claim = self.claim()
                phases.append(claim.claim_phase)
                (seen_old if claim.claim_phase == 'reconcile' else seen_new).append(claim.task.id)
                self.finish(claim)
                # Reopen the store to prove the turn survives caller restarts.
                self.store = XPostAutoStore(self.f.db_path, now_fn=self.f.clock)
            self.assertEqual(phases, ['reconcile', 'selection'] * (len(phases) // 2))
            self.f.clock.value += timedelta(seconds=60)
        self.assertEqual(seen_old, observations)
        self.assertEqual(seen_new, progress)

    def test_empty_lane_uses_all_available_capacity(self):
        tasks = [self.task().id for _ in range(3)]
        for expected in tasks:
            claim = self.claim()
            self.assertEqual(claim.task.id, expected)
            self.finish(claim)
        self.assertIsNone(self.claim())
        for _ in range(3):
            task = self.task(True)
            claim = self.claim()
            self.assertEqual(claim.task.id, task.id)
            self.assertEqual(claim.claim_phase, 'reconcile')
            self.finish(claim)

    def test_holds_future_schedules_backoff_and_active_leases_stay_ineligible(self):
        held, future = self.task(), self.task()
        delayed, leased = self.task(True, due_offset=300), self.task(True)
        with self.store._transaction() as conn:
            conn.execute("INSERT INTO pipeline_canonical_hold VALUES(?,?,?)",
                         ('x:auto:' + str(held.id), self.f.clock().isoformat(), 'test'))
            stamp = (self.f.clock() + timedelta(hours=1)).isoformat()
            conn.execute('UPDATE x_auto_task SET scheduled_at_utc=? WHERE id=?', (stamp, future.id))
            conn.execute("UPDATE x_auto_task SET claim_token='active',lease_expires_at_utc=? WHERE id=?", (stamp, leased.id))
        self.assertIsNone(self.claim())
        with self.store._reader() as conn:
            self.assertEqual(conn.execute('SELECT claim_token FROM x_auto_task WHERE id=?',
                                          (leased.id,)).fetchone()[0], 'active')
        self.assertEqual(self.store.get_task(delayed.id).status, 'retry_wait')

    def test_closed_gates_only_reconcile_and_expired_publication_never_republishes(self):
        pending = self.task()
        unknown = self.task(True, status='publishing')
        claim = self.claim(reconcile_only=True)
        self.assertEqual(claim.task.id, unknown.id)
        self.assertEqual(claim.claim_phase, 'reconcile')
        self.assertTrue(claim.task.unknown_outcome)
        with self.store._reader() as conn:
            self.assertEqual(conn.execute('SELECT publish_attempt_count FROM x_auto_task WHERE id=?',
                                          (unknown.id,)).fetchone()[0], 0)
        self.finish(claim)
        self.assertIsNone(self.claim(reconcile_only=True))
        self.assertEqual(self.claim().task.id, pending.id)

    def test_cutoff_is_not_bypassed_and_legacy_priority_is_unchanged(self):
        pending = self.task()
        old = self.task(True)
        claim = self.claim()
        self.finish(claim)
        cutoff = self.f.clock() + timedelta(seconds=1)
        self.assertIsNone(self.claim(planning_cutoff=cutoff))
        self.f.clock.value += timedelta(seconds=60)
        legacy = self.store.claim_next_executable_task(worker_id='legacy', lease_seconds=120)
        self.assertEqual(legacy.task.id, old.id)
        self.assertEqual(legacy.claim_phase, 'reconcile')
        self.assertEqual(self.store.get_task(pending.id).status, 'pending')

    def test_concurrent_callers_share_turn_and_never_claim_same_task(self):
        for _ in range(4): self.task(True)
        for _ in range(4): self.task()
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: self.claim(), range(8)))
        self.assertEqual(len({claim.task.id for claim in claims}), 8)
        self.assertEqual(sorted(claim.claim_phase for claim in claims), ['reconcile'] * 4 + ['selection'] * 4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
