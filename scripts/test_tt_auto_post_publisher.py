#!/usr/bin/env python3
"""End-to-end offline tests for one TT auto-post account task.

All remote collaborators are fakes.  No test can reach GPU or TikTok.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_auto_posts.core import (  # noqa: E402
    AuditActor,
    TTPostAutoStore,
)
from features.tt_auto_posts.publisher import (  # noqa: E402
    AutoLiveGates,
    AutoPostExecutionError,
    AutoPostExecutor,
)
from features.tt_posts.core import PublishCredentials, SafeAccount  # noqa: E402
from features.tt_posts.service import GPUClientError  # noqa: E402


UTC = timezone.utc


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class UnusedSelector:
    """Reserved-task tests must never invoke candidate selection."""

    source = object()
    metrics = object()
    legacy_reader = object()
    material_validator = None
    product = "Dramawave"
    app_id = 1479
    material_data_source = 6


class FakeAccountSource:
    def __init__(self):
        self.tokens_opened = 0
        self.username = "account640"
        self.display_name = "Account 640"

    def publish_credentials(self, account_id):
        self.tokens_opened += 1
        account = SafeAccount(
            account_id=str(account_id),
            username=self.username,
            display_name=self.display_name,
            avatar_url="",
            status="active",
            publish_eligible=True,
        )
        return PublishCredentials(account, "fake-test-token-never-logged")


class FakeGPU:
    def __init__(self, *, prepare_failures=0, publish_results=None, reconcile_result=None):
        self.prepare_failures = int(prepare_failures)
        self.publish_results = list(publish_results or [])
        self.reconcile_result = reconcile_result or {
            "publish_id": "pub-default",
            "state": "published",
            "publish_url": "https://www.tiktok.com/@account640/video/1",
        }
        self.prepare_calls = []
        self.creator_calls = []
        self.publish_calls = []
        self.reconcile_calls = []

    def prepare(self, **kwargs):
        self.prepare_calls.append(dict(kwargs))
        if len(self.prepare_calls) <= self.prepare_failures:
            raise AutoPostExecutionError(
                "tt_auto_fake_prepare_unavailable",
                "temporary preparation failure",
                503,
            )
        return {
            "job_id": kwargs["job_id"],
            "content_id": kwargs["material"]["content_id"],
            "profile": kwargs["expected_profile"],
            "output_url": "https://media.example.test/prepared.mp4",
            "output_sha256": "a" * 64,
            "output_size": 123456,
            "probe": {"duration": 28.25},
        }

    def creator_info(self, **kwargs):
        self.creator_calls.append(dict(kwargs))
        return {
            "privacy_level_options": ["PUBLIC_TO_EVERYONE"],
            "comment_disabled": False,
            "duet_disabled": False,
            "stitch_disabled": False,
            "max_video_post_duration_sec": 600,
        }

    def publish(self, **kwargs):
        self.publish_calls.append(dict(kwargs))
        if self.publish_results:
            value = self.publish_results.pop(0)
        else:
            value = {
                "publish_id": "pub-success",
                "state": "published",
                "publish_url": "https://www.tiktok.com/@account640/video/2",
            }
        if isinstance(value, BaseException):
            raise value
        return value

    def reconcile(self, **kwargs):
        self.reconcile_calls.append(dict(kwargs))
        if isinstance(self.reconcile_result, BaseException):
            raise self.reconcile_result
        return dict(self.reconcile_result)


class FakeCodeBroker:
    def __init__(self, code="AB12"):
        self.code = code
        self.freeze_calls = []
        self.state_calls = []

    def freeze_route(self, task_id, **kwargs):
        self.freeze_calls.append({"task_id": int(task_id), **dict(kwargs)})
        return self.code

    def set_state(self, task_id, **kwargs):
        self.state_calls.append({"task_id": int(task_id), **dict(kwargs)})
        return {"task_id": int(task_id), "code": self.code, "state": kwargs["state"]}


def template_config():
    return {
        "account_ids": ["640"],
        "caption_template": "Drama {{content_id}}\n{desc}\n{url}",
        "metric_window_days": 7,
        "drama_launch_window_days": 0,
        "cooldown_days": 0,
        "platform": 0,
        "drama_rule": {
            "spend_min": None,
            "spend_max": None,
            "roas_min": None,
            "roas_max": None,
            "sort_by": "spend",
            "sort_direction": "desc",
            "resource_type_v2": ["1"],
        },
        "material_rule": {
            "spend_min": None,
            "spend_max": None,
            "roas_min": None,
            "roas_max": None,
            "sort_by": "spend",
            "sort_direction": "desc",
            "duration_min_seconds": 1,
            "duration_max_seconds": 120,
        },
        "schedule": {"mode": "fixed", "times": ["18:00"]},
    }


class TTAutoPostPublisherIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = MutableClock(datetime(2026, 8, 5, 10, 0, tzinfo=UTC))
        self.store = TTPostAutoStore(
            Path(self.temp.name) / "tt-auto.sqlite3",
            now_fn=self.clock,
        )
        self.account_source = FakeAccountSource()

    def reserved_task(
        self,
        *,
        suffix="1",
        caption_template=None,
        account_username="account640",
    ):
        config = template_config()
        if caption_template is not None:
            config["caption_template"] = caption_template
        template = self.store.create_template(
            name="Template " + suffix,
            config=config,
            actor=AuditActor("803", "operator"),
            confirmation={"accepted": True},
        )
        run = self.store.create_run(
            run_key="publisher-test-" + suffix,
            template_id=template.id,
            template_version=template.version,
            trigger_type="manual",
            scheduled_at_utc=self.clock(),
            shanghai_date="2026-08-05",
            publish_time="18:00",
            blacklist_snapshot={"sha256": "b" * 64},
            actor=AuditActor("803", "operator"),
        )
        task = self.store.create_task(
            run_id=run.id,
            account_id="640",
            account_username=account_username,
            account_display_name="Account 640",
            drama_language="en",
            account_setting_version=3,
            account_settings={
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "allow_comment": True,
                "allow_duet": False,
                "allow_stitch": False,
                "brand_content_toggle": False,
                "brand_organic_toggle": False,
                "is_aigc": True,
            },
        )
        self.store.reserve_material(
            task_id=task.id,
            material_id="M-" + suffix,
            content_id="C-" + suffix,
            series_code="S-" + suffix,
            reserved_at_utc=self.clock(),
            selection_snapshot={
                "drama": {"name": "Drama " + suffix},
                "material": {
                    "source_media_url": "https://media.example.test/source-%s.mp4" % suffix,
                    "material_name": "clip-%s.mp4" % suffix,
                    "drama_name": "Drama " + suffix,
                    "material_tag": "hook",
                    "description": "Description " + suffix,
                },
            },
        )
        return self.store.get_task(task.id)

    def executor(self, gpu, code_broker=None):
        return AutoPostExecutor(
            self.store,
            UnusedSelector(),
            self.account_source,
            gpu,
            code_route_broker=code_broker,
            gates=AutoLiveGates(True, True, True),
            now_fn=self.clock,
            short_link_root=Path(self.temp.name).resolve() / "s2l",
            lease_seconds=120,
        )

    def test_prepare_then_publish_completes_once(self):
        task = self.reserved_task(suffix="success")
        gpu = FakeGPU()
        executor = self.executor(gpu)
        prepared = executor.execute_next("worker-1")
        self.assertTrue(prepared["claimed"])
        self.assertEqual(prepared["task"]["status"], "ready")
        self.assertEqual(len(gpu.prepare_calls), 1)
        self.assertEqual(len(gpu.publish_calls), 0)

        result = executor.execute_next("worker-2")
        final = self.store.get_task(task.id)
        self.assertTrue(result["claimed"])
        self.assertEqual(final.status, "published")
        self.assertEqual(final.publish_id, "pub-success")
        self.assertEqual(len(gpu.prepare_calls), 1)
        self.assertEqual(len(gpu.publish_calls), 1)
        self.assertEqual(len(gpu.reconcile_calls), 0)
        self.assertEqual(
            gpu.publish_calls[0]["queue"]["privacy_level"],
            "PUBLIC_TO_EVERYONE",
        )
        self.assertIs(gpu.publish_calls[0]["queue"]["allow_comment"], True)
        self.assertIs(gpu.publish_calls[0]["queue"]["allow_duet"], False)
        self.assertIn("/s2l/tt-auto/%d.html" % task.id, final.caption)
        self.assertTrue(
            (Path(self.temp.name) / "s2l" / "tt-auto" / ("%d.html" % task.id)).is_file()
        )

    def test_snapshot_external_account_identity_does_not_block_preparation(self):
        task = self.reserved_task(
            suffix="snapshot-external-id",
            account_username="-000iaALn26DdasX2CjKe_cxuOJ-2etojsT_",
        )
        result = self.executor(FakeGPU()).execute_next("worker-snapshot-id")
        self.assertTrue(result["claimed"])
        self.assertEqual(result["task"]["status"], "ready")
        self.assertEqual(self.store.get_task(task.id).error_code, "")

    def test_code_is_frozen_before_prepare_and_reused_for_publish_retry(self):
        task = self.reserved_task(
            suffix="code-retry",
            caption_template="Find the full story with {code}\n{desc}\n{url}",
        )
        temporary = GPUClientError(
            "tt_publish_upstream_unavailable",
            "publish was definitely not initialized",
            503,
            publish_was_not_created=True,
        )
        gpu = FakeGPU(publish_results=[temporary])
        broker = FakeCodeBroker("Q7M2")
        executor = self.executor(gpu, broker)

        prepared = executor.execute_next("worker-code-prepare")["task"]
        self.assertEqual(prepared["status"], "ready")
        self.assertIn("Q7M2", prepared["caption"])
        self.assertNotIn("{code}", prepared["caption"])
        frozen_caption = prepared["caption"]
        self.assertEqual(len(gpu.prepare_calls), 1)

        failed = executor.execute_next("worker-code-publish-1")["task"]
        self.assertEqual(failed["status"], "retry_wait")
        self.clock.value += timedelta(minutes=5, seconds=1)
        published = executor.execute_next("worker-code-publish-2")["task"]

        self.assertEqual(published["status"], "published")
        self.assertEqual(published["caption"], frozen_caption)
        self.assertEqual(
            [call["queue"]["caption"] for call in gpu.publish_calls],
            [frozen_caption, frozen_caption],
        )
        self.assertGreaterEqual(len(broker.freeze_calls), 3)
        self.assertEqual(
            [call["state"] for call in broker.state_calls],
            ["publishing", "publishing", "published"],
        )

    def test_ready_is_reclaimed_and_inflight_publish_cannot_be_stolen(self):
        task = self.reserved_task(suffix="publish-fence")
        gpu = FakeGPU()
        executor = self.executor(gpu)
        prepared = executor.execute_next("worker-prepare")["task"]
        self.assertEqual(prepared["status"], "ready")

        publish_started = threading.Event()
        publish_release = threading.Event()
        original_publish = gpu.publish

        def blocking_publish(**kwargs):
            publish_started.set()
            if not publish_release.wait(timeout=5):
                raise RuntimeError("test publish release timed out")
            return original_publish(**kwargs)

        gpu.publish = blocking_publish
        results = []
        errors = []

        def run_publish():
            try:
                results.append(executor.execute_next("worker-publish"))
            except BaseException as exc:  # pragma: no cover - test diagnostics
                errors.append(exc)

        thread = threading.Thread(target=run_publish)
        thread.start()
        self.assertTrue(publish_started.wait(timeout=3))
        self.assertEqual(self.store.get_task(task.id).status, "publishing")

        competing = executor.execute_next("worker-competing")
        self.assertFalse(competing["claimed"])
        self.assertEqual(len(gpu.reconcile_calls), 0)

        publish_release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["task"]["status"], "published")
        self.assertEqual(self.store.get_task(task.id).status, "published")

    def test_transient_prepare_failure_retries_same_material_and_gpu_job(self):
        task = self.reserved_task(suffix="retry")
        gpu = FakeGPU(prepare_failures=1)
        executor = self.executor(gpu)
        first = executor.execute_next("worker-1")["task"]
        self.assertEqual(first["status"], "retry_wait")
        self.assertEqual(first["material_id"], task.material_id)
        first_job = first["gpu_job_id"]
        self.assertTrue(first_job)

        self.clock.value += timedelta(minutes=5, seconds=1)
        second = executor.execute_next("worker-1")["task"]
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["material_id"], task.material_id)
        self.assertEqual(second["gpu_job_id"], first_job)
        third = executor.execute_next("worker-2")["task"]
        self.assertEqual(third["status"], "published")
        self.assertEqual(third["material_id"], task.material_id)
        self.assertEqual(
            [call["job_id"] for call in gpu.prepare_calls],
            [first_job, first_job],
        )
        self.assertTrue(self.store.material_is_reserved(task.material_id))

    def test_transient_selection_failure_waits_and_does_not_become_terminal(self):
        template = self.store.create_template(
            name="Selection retry",
            config=template_config(),
            actor=AuditActor("803", "operator"),
            confirmation={"accepted": True},
        )
        run = self.store.create_run(
            run_key="selection-retry-test",
            template_id=template.id,
            template_version=template.version,
            trigger_type="manual",
            scheduled_at_utc=self.clock(),
            shanghai_date="2026-08-05",
            publish_time="18:00",
            blacklist_snapshot={"sha256": "b" * 64},
            actor=AuditActor("803", "operator"),
        )
        task = self.store.create_task(
            run_id=run.id,
            account_id="640",
            account_username="account640",
            account_display_name="Account 640",
            drama_language="en",
            account_setting_version=3,
            account_settings={"privacy_level": "PUBLIC_TO_EVERYONE"},
        )
        claim = self.store.claim_next_executable_task(
            worker_id="worker-1",
            lease_seconds=120,
            now=self.clock(),
        )
        self.assertIsNotNone(claim)
        retried = self.executor(FakeGPU())._retry_or_fail(
            claim.task,
            "selection",
            AutoPostExecutionError(
                "tt_auto_metric_window_not_ready",
                "metric cache not ready",
                503,
            ),
            claim.reveal_claim_token(),
        )
        self.assertEqual(retried.status, "retry_wait")
        self.assertEqual(retried.material_id, "")
        self.assertEqual(self.store.get_run(run.id).status, "queued")

    def test_definite_publish_failure_retries_same_material_without_reprepare(self):
        task = self.reserved_task(suffix="publish-retry")
        temporary = GPUClientError(
            "tt_publish_upstream_unavailable",
            "publish was definitely not initialized",
            503,
            publish_was_not_created=True,
        )
        gpu = FakeGPU(publish_results=[temporary])
        executor = self.executor(gpu)
        first = executor.execute_next("worker-1")["task"]
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["material_id"], task.material_id)
        job_id = first["gpu_job_id"]

        second = executor.execute_next("worker-2")["task"]
        self.assertEqual(second["status"], "retry_wait")

        self.clock.value += timedelta(minutes=5, seconds=1)
        third = executor.execute_next("worker-1")["task"]
        self.assertEqual(third["status"], "published")
        self.assertEqual(third["material_id"], task.material_id)
        self.assertEqual(third["gpu_job_id"], job_id)
        self.assertEqual(len(gpu.prepare_calls), 1)
        self.assertEqual(len(gpu.publish_calls), 2)
        self.assertEqual(
            [call["job_id"] for call in gpu.publish_calls],
            [job_id, job_id],
        )

    def test_retry_keeps_short_link_identical_after_account_name_changes(self):
        task = self.reserved_task(suffix="frozen-account-name")
        temporary = GPUClientError(
            "tt_publish_upstream_unavailable",
            "publish was definitely not initialized",
            503,
            publish_was_not_created=True,
        )
        gpu = FakeGPU(publish_results=[temporary])
        executor = self.executor(gpu)
        first = executor.execute_next("worker-1")["task"]
        self.assertEqual(first["status"], "ready")
        second = executor.execute_next("worker-2")["task"]
        self.assertEqual(second["status"], "retry_wait")
        link_path = (
            Path(self.temp.name)
            / "s2l"
            / "tt-auto"
            / ("%d.html" % task.id)
        )
        original = link_path.read_bytes()

        self.account_source.username = "renamed-account640"
        self.account_source.display_name = "Renamed Account 640"
        self.clock.value += timedelta(minutes=5, seconds=1)
        third = executor.execute_next("worker-1")["task"]

        self.assertEqual(third["status"], "published")
        self.assertEqual(link_path.read_bytes(), original)
        self.assertEqual(len(gpu.publish_calls), 2)

    def test_unknown_publish_outcome_reconciles_without_reprepare_or_republish(self):
        task = self.reserved_task(suffix="unknown")
        unknown = GPUClientError(
            "tt_publish_timeout",
            "outcome unknown",
            504,
            unknown_outcome=True,
        )
        gpu = FakeGPU(
            publish_results=[unknown],
            reconcile_result={
                "publish_id": "pub-recovered",
                "state": "published",
                "publish_url": "https://www.tiktok.com/@account640/video/3",
            },
        )
        executor = self.executor(gpu)
        first = executor.execute_next("worker-1")["task"]
        self.assertEqual(first["status"], "ready")

        second = executor.execute_next("worker-1")["task"]
        self.assertEqual(second["status"], "unknown")
        self.assertTrue(second["unknown_outcome"])

        third = executor.execute_next("worker-1")["task"]
        self.assertEqual(third["status"], "published")
        self.assertEqual(third["publish_id"], "pub-recovered")
        self.assertEqual(len(gpu.prepare_calls), 1)
        self.assertEqual(len(gpu.publish_calls), 1)
        self.assertEqual(len(gpu.reconcile_calls), 1)
        self.assertEqual(third["material_id"], task.material_id)

    def test_reconcile_4xx_remains_nonterminal_until_remote_result_is_known(self):
        task = self.reserved_task(suffix="reconcile-4xx")
        unknown = GPUClientError(
            "tt_publish_timeout",
            "outcome unknown",
            504,
            unknown_outcome=True,
        )
        reconcile_unavailable = AutoPostExecutionError(
            "tt_auto_reconcile_temporarily_unavailable",
            "reconcile is temporarily unavailable",
            409,
        )
        gpu = FakeGPU(
            publish_results=[unknown],
            reconcile_result=reconcile_unavailable,
        )
        executor = self.executor(gpu)
        first = executor.execute_next("worker-1")["task"]
        self.assertEqual(first["status"], "ready")

        second = executor.execute_next("worker-1")["task"]
        self.assertEqual(second["status"], "unknown")
        third = executor.execute_next("worker-1")["task"]
        self.assertEqual(third["status"], "retry_wait")
        self.assertTrue(third["unknown_outcome"])
        self.assertEqual(len(gpu.publish_calls), 1)

        gpu.reconcile_result = {
            "publish_id": "pub-reconcile-4xx",
            "state": "published",
            "publish_url": "https://www.tiktok.com/@account640/video/5",
        }
        self.clock.value += timedelta(minutes=5, seconds=1)
        fourth = executor.execute_next("worker-1")["task"]
        self.assertEqual(fourth["status"], "published")
        self.assertEqual(fourth["publish_id"], "pub-reconcile-4xx")
        self.assertEqual(len(gpu.publish_calls), 1)
        self.assertEqual(len(gpu.reconcile_calls), 2)

    def test_recorded_publish_id_only_reconciles_on_followup(self):
        task = self.reserved_task(suffix="publish-id")
        gpu = FakeGPU(
            publish_results=[{"publish_id": "pub-pending", "state": "processing"}],
            reconcile_result={
                "publish_id": "pub-pending",
                "state": "published",
                "publish_url": "https://www.tiktok.com/@account640/video/4",
            },
        )
        executor = self.executor(gpu)
        first = executor.execute_next("worker-1")["task"]
        self.assertEqual(first["status"], "ready")

        second = executor.execute_next("worker-1")["task"]
        self.assertEqual(second["status"], "reconciling")
        self.assertEqual(second["publish_id"], "pub-pending")

        third = executor.execute_next("worker-1")["task"]
        self.assertEqual(third["status"], "published")
        self.assertEqual(len(gpu.prepare_calls), 1)
        self.assertEqual(len(gpu.publish_calls), 1)
        self.assertEqual(len(gpu.reconcile_calls), 1)
        self.assertEqual(third["material_id"], task.material_id)

    def test_closed_live_gates_never_claim_or_call_gpu(self):
        task = self.reserved_task(suffix="held")
        gpu = FakeGPU()
        executor = AutoPostExecutor(
            self.store,
            UnusedSelector(),
            self.account_source,
            gpu,
            gates=AutoLiveGates(),
            now_fn=self.clock,
            short_link_root=Path(self.temp.name).resolve() / "s2l",
            lease_seconds=120,
        )
        result = executor.execute_next("worker-1")
        self.assertEqual(result["held"], "live_gates_closed")
        self.assertEqual(self.store.get_task(task.id).status, "reserved")
        self.assertEqual(gpu.prepare_calls, [])
        self.assertEqual(gpu.publish_calls, [])


if __name__ == "__main__":
    unittest.main()
