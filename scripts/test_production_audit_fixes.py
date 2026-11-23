"""Selection handoff, expired read-only selection, and image template routing."""
import datetime as dt
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.pipeline_dispatch import media_gate, native_media
from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from features.pipeline_dispatch.core import DispatchStore
from features.pipeline_dispatch.service import Coordinator
from features.cover_material_source import country_language
from scripts import test_pipeline_tt_selection_recovery as tt_fixtures
from scripts import test_pipeline_x_native as x_fixtures


class SelectionTests(unittest.TestCase):
    def fixture(self):
        assets = patch('features.pipeline_dispatch.tt_native.outro_capabilities', return_value=[])
        assets.start(); self.addCleanup(assets.stop)
        return tt_fixtures.TTSelectionRecoveryTests.fixture(self)

    def test_media_selection_then_render_reservation_keeps_exact_identity(self):
        f = self.fixture()
        central = DispatchStore(Path(f.fixture.temp.name) / 'central.sqlite', f.fixture.clock)
        central.channel_enabled('tt', True)
        job = central.offer(f.offer)
        ticket, decision = 'selection-ticket', {'kind': 'select_source', 'deadline': '', 'publish_at': ''}
        with central.transaction() as c:
            c.execute("INSERT OR REPLACE INTO pipeline_media_gate(source_key,phase,ticket,decision) VALUES(?,'pending',?,?)",
                      (job['source_key'], ticket, json.dumps(decision)))
            # Older observers mislabeled a media-gate selection as legacy work.
            c.execute("UPDATE pipeline_job SET status='legacy_running',hold_reason=? WHERE id=?", (media_gate.HOLD, job['id']))
        f.adapter.driver.call = f.bridge.dispatch
        coordinator = Coordinator(central, f.registry, internal_token='i'*32, master_key='m'*32)
        coordinator.deliver_media(job, ticket, decision)
        refreshed = central.get_job(job['id'])
        self.assertEqual(refreshed['payload']['canonical_fingerprint'], f.adapter.project(f.adapter.read(f.task.id))['canonical_fingerprint'])
        self.assertEqual(refreshed['generation'], 0)
        self.assertEqual(refreshed['status'], 'queued')
        self.assertEqual(f.selected_calls, [f.task.id])
        descriptor = f.registry.reserve(dict(refreshed, generation=1, executor_backend='cpu'), f.claim)
        self.assertEqual(descriptor['kind'], 'tt_prepare')
        self.assertEqual(f.selected_calls, [f.task.id])
        self.assertFalse(f.gpu.prepare_calls or f.gpu.publish_calls)

    def test_expired_selection_reclaims_only_the_original_task(self):
        f = self.fixture()
        claim = f.store.claim_next_executable_task(worker_id='pipeline:media-selection', lease_seconds=180,
            prepare_ahead_seconds=f.executor.prepare_ahead_seconds, allowed_phases={'selection'}, task_id=f.task.id)
        self.assertIsNotNone(claim)
        self.assertFalse(f.adapter.project(f.adapter.read(f.task.id))['legacy_inflight'])
        result = native_media._selected_tt(f.bridge, f.job, f.adapter.read(f.task.id))
        self.assertTrue(result['retry'])
        self.assertEqual(f.selected_calls, [])
        f.fixture.clock.value += dt.timedelta(seconds=181)
        self.assertIsNone(native_media._selected_tt(f.bridge, f.job, f.adapter.read(f.task.id)))
        self.assertEqual(f.store.get_task(f.task.id).status, 'reserved')
        self.assertEqual(f.selected_calls, [f.task.id])

    def test_selection_heartbeat_renews_while_selector_is_working(self):
        seen = threading.Event()
        store = SimpleNamespace(renew_task_claim=lambda *a, **kw: seen.set())
        with native_media._tt_selection_lease(store, 1, 'token', interval=.01):
            self.assertTrue(seen.wait(1))

    def test_claimed_task_never_adopts_selection_observation(self):
        f = self.fixture()
        central = DispatchStore(Path(f.fixture.temp.name) / 'central.sqlite', f.fixture.clock)
        job = central.offer(f.offer)
        with central.transaction() as c:
            c.execute("UPDATE pipeline_job SET generation=1,status='running',claim_hash='owned' WHERE id=?", (job['id'],))
        changed = dict(f.offer, tt_selection_from_fingerprint=f.offer['canonical_fingerprint'], canonical_fingerprint='changed')
        central.offer(changed)
        self.assertEqual(central.get_job(job['id'])['payload']['canonical_fingerprint'], f.offer['canonical_fingerprint'])


class ImageRoutingTests(unittest.TestCase):
    def test_random_images_complete_and_publish_with_original_template_flag(self):
        f = x_fixtures.NativeTests(); f.setUp(); self.addCleanup(f.doCleanups)
        links = patch.object(f.bridge, '_links', side_effect=lambda queue, log: log)
        links.start(); self.addCleanup(links.stop)
        queue = f.queue(random=True)
        job, _, artifact = f.preflight(queue, mime='image/png', duration=0)
        result = f.bridge.complete(job, {'artifact': artifact}, job)
        self.assertEqual(result['status'], 'ready')
        adapter = LedgerAdapter(LedgerSpec('x', 'queue', str(f.runtime.POST_DB_PATH), 'x_post_queue'))
        row = adapter.read(queue['id'])
        self.assertEqual(row['random_template_enabled'], 1)
        self.assertEqual(adapter.project(row)['stage'], 'publish')
        published = f.bridge.reserve(f.job(queue, 'publish'), f.job(queue, 'publish'))
        self.assertEqual(published['request']['media_category'], 'tweet_image')
        self.assertEqual(published['artifact'], artifact)
        with f.bridge.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM x_post_random_template_preparation').fetchone()[0], 0)

    def test_missing_video_template_result_still_fails(self):
        f = x_fixtures.NativeTests(); f.setUp(); self.addCleanup(f.doCleanups)
        queue = f.queue(random=True)
        job, _, artifact = f.preflight(queue)
        with self.assertRaises(Exception):
            f.bridge.complete(job, {'artifact': artifact}, job)


class LanguageTests(unittest.TestCase):
    def test_hi_ms_and_explicit_country(self):
        self.assertEqual(country_language('', 'hi-IN'), ('IN', 'hi'))
        self.assertEqual(country_language('', 'ms_MY'), ('MY', 'ms'))
        self.assertEqual(country_language('SG', 'ms'), ('SG', 'ms'))


if __name__ == '__main__':
    unittest.main()
