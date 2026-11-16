"""Restricted channels remain selected and are excluded at execution time."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from features.x_auto_posts.publisher import AutoLiveGates
from features.x_auto_posts.service import AutoPostServiceError
from features.x_posts.service import BEIJING_TZ, XPostError, XPostStore
from scripts import test_x_auto_post_service as auto_tests
from scripts.test_x_auto_post_validation import valid_payload


class ChannelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = auto_tests.XAutoPostServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service(AutoLiveGates(True, True, True))

    def create(self, ids=("101", "102")):
        return self.service.create_template(self.fixture.actor(
            valid_payload(account_ids=list(ids))))["template"]

    def submit_run(self, template, key):
        return self.service.run_now(template["id"], self.fixture.actor({
            "expected_version": template["version"], "confirmed": True,
            "idempotency_key": key,
        }))

    def test_save_update_and_enable_preserve_restricted_accounts(self):
        self.fixture.bridge.accounts_by_id["102"].update(
            status="error", publish_eligible=False)
        created = self.create()
        updated = self.service.update_template(created["id"], self.fixture.actor({
            **valid_payload(account_ids=["101", "102"]), "expected_version": 1,
        }))["template"]
        enabled = self.service.set_enabled(updated["id"], True,
            self.fixture.actor({"expected_version": updated["version"]}))["template"]
        self.assertEqual(enabled["config"]["account_ids"], ["101", "102"])
        self.assertTrue(enabled["enabled"])
        self.assertEqual(self.fixture.bridge.verify_calls, [])

    def test_run_skips_restricted_channel_without_taking_material(self):
        self.fixture.bridge.accounts_by_id["102"]["publish_eligible"] = False
        template = self.create()
        first = self.submit_run(template, "mixed-channel-0001")
        second = self.submit_run(template, "mixed-channel-0001")
        self.assertTrue(second["idempotent"])
        tasks = self.fixture.store.list_tasks(run_id=first["run"]["id"])
        self.assertEqual([(t.account_id, t.status) for t in tasks],
                         [("101", "pending"), ("102", "skipped")])
        self.assertEqual(tasks[1].error_code, "x_auto_account_not_publishable")
        self.assertFalse(tasks[1].material_id)
        self.assertFalse(tasks[1].execution_queue_id)
        self.assertEqual([c[0] for c in self.fixture.bridge.verify_calls], [101, 102])
        self.assertEqual(self.fixture.store.get_template(template["id"]).config["account_ids"],
                         ["101", "102"])

    def test_mixed_scope_healthy_channel_reaches_confirmed_publish_once(self):
        from features.x_auto_posts.publisher import AutoPostExecutor
        from scripts import test_x_auto_post_publisher as publisher_fixtures

        self.fixture.bridge.accounts_by_id["102"]["publish_eligible"] = False
        template = self.create()
        submitted = self.submit_run(template, "mixed-confirmed-publish-0001")
        store = self.fixture.store
        claim = store.claim_next_executable_task(
            worker_id="channel-selector", lease_seconds=120, now=self.fixture.clock())
        self.assertEqual(claim.task.account_id, "101")
        token = claim.reveal_claim_token()
        store.reserve_material(task_id=claim.task.id, material_id="123456",
            content_id="DramaOne", selection={"material": {
                "material_id": "123456", "video_duration": 120}}, claim_token=token)
        store.confirm_material_reservation(claim.task.id, 9002, claim_token=token)
        ready = store.transition_task(claim.task.id, "ready", expected_statuses={"reserved"},
            claim_token=token, updates={"execution_run_id": 9001,
                "execution_queue_id": 9002, "selected_duration_sec": 120,
                "claim_phase": ""})
        bridge = publisher_fixtures.FakeBridge(ready)

        def publish(queue_id):
            self.assertEqual(queue_id, 9002)
            bridge.publish_calls += 1
            bridge.queue_status = "published"
            return {"status": "published"}

        bridge.publish_queue = publish
        executor = AutoPostExecutor(store, publisher_fixtures.DummySelector(), bridge,
            lambda *_: {}, lambda *_: {}, gates=AutoLiveGates(True, True, True),
            now_fn=self.fixture.clock, lease_seconds=120)
        result = executor.execute_next("channel-publisher")
        self.assertEqual(result["task"]["status"], "published")
        self.assertEqual(result["task"]["publish_id"], "1900000000000000000")
        self.assertEqual(bridge.publish_calls, 1)
        self.assertEqual(store.get_run(submitted["run"]["id"]).status, "completed")
        skipped = next(t for t in store.list_tasks(run_id=submitted["run"]["id"])
                       if t.account_id == "102")
        self.assertEqual(skipped.status, "skipped")
        self.assertFalse(skipped.material_id)
        self.assertIsNone(store.get_task_reservation(skipped.id))
        self.assertFalse(executor.execute_next("channel-publisher-again")["claimed"])
        self.assertEqual(bridge.publish_calls, 1)

    def test_all_restricted_finish_as_skipped_and_recovered_channel_rejoins(self):
        self.fixture.bridge.accounts_by_id["101"]["publish_eligible"] = False
        template = self.create(("101",))
        first = self.submit_run(template, "all-restricted-0001")
        self.assertEqual(self.fixture.store.get_run(first["run"]["id"]).status, "completed")
        self.assertEqual(self.fixture.store.list_tasks(run_id=first["run"]["id"])[0].status,
                         "skipped")
        self.fixture.bridge.accounts_by_id["101"]["publish_eligible"] = True
        second = self.submit_run(template, "recovered-channel-0002")
        self.assertEqual(self.fixture.store.list_tasks(run_id=second["run"]["id"])[0].status,
                         "pending")

    def test_restriction_found_during_verify_skips_only_that_channel(self):
        template = self.create()
        original = self.fixture.bridge.verify_account

        def verify(account_id, **options):
            if str(account_id) == "102":
                raise AutoPostServiceError("x_account_suspended", "suspended", 409)
            return original(account_id, **options)

        with mock.patch.object(self.fixture.bridge, "verify_account", side_effect=verify):
            result = self.submit_run(template, "verify-restriction-0001")
        self.assertEqual([t.status for t in self.fixture.store.list_tasks(
            run_id=result["run"]["id"])], ["pending", "skipped"])

    def test_unknown_error_is_not_classified_as_safe_skip(self):
        template = self.create(("101",))
        error = AutoPostServiceError("x_post_account_locked", "unknown", 409)
        error.unknown_outcome = True
        with mock.patch.object(self.fixture.bridge, "verify_account", side_effect=error):
            result = self.submit_run(template, "unknown-channel-0001")
        self.assertEqual(self.fixture.store.list_tasks(run_id=result["run"]["id"])[0].status,
                         "failed")

    def test_missing_account_still_fails_save(self):
        with self.assertRaises(AutoPostServiceError) as caught:
            self.create(("99999",))
        self.assertEqual(caught.exception.code, "x_auto_account_not_found")


class PoolSelectionTests(unittest.TestCase):
    def test_runner_filters_current_restrictions_and_keeps_healthy_channels(self):
        from scripts import x_post_schedule_runner as runner
        from scripts import test_x_post_schedule_runner as fixtures
        for code in ("x_account_not_publishable", "x_account_suspended",
                     "x_post_account_locked", "x_post_account_needs_review",
                     "x_account_publish_not_approved", "x_token_missing"):
            with self.subTest(code=code):
                sidecar = fixtures.FakeSidecar([])
                original = sidecar.verify_account

                def verify(account_id):
                    if account_id == 12:
                        raise runner.SidecarError(code, "restricted", 409)
                    return original(account_id)

                skipped = []
                with mock.patch.object(sidecar, "verify_account", side_effect=verify):
                    accounts = runner._verify_accounts(sidecar, [11, 12],
                        skip_blocked=True, skipped_accounts=skipped)
                self.assertEqual([a["id"] for a in accounts], [11])
                self.assertEqual([(a["account_id"], a["error_code"]) for a in skipped],
                                 [(12, code)])

    def test_material_and_drama_keep_full_scope_even_when_all_restricted(self):
        for source in ("material", "drama"):
            for ids in ([101, 102], [102]):
                with self.subTest(source=source, ids=ids), tempfile.TemporaryDirectory() as tmp:
                    store = XPostStore(Path(tmp) / "posts.sqlite3")
                    saved = store.save_schedule_config(source, {
                        "enabled": True, "account_ids": ids, "publish_times": ["12:00"],
                        "version": 1,
                    }, known_account_ids=[101, 102], eligible_account_ids=[101],
                        skippable_account_ids=[102],
                        now=datetime(2026, 11, 16, 8, tzinfo=BEIJING_TZ))
                    self.assertEqual(saved["account_ids"], ids)
                    self.assertTrue(saved["enabled"])
                    self.assertNotIn("skipped_account_ids", saved)

    def test_missing_pool_account_still_fails_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = XPostStore(Path(tmp) / "posts.sqlite3")
            with self.assertRaises(XPostError) as caught:
                store.save_schedule_config("material", {
                    "enabled": True, "account_ids": [999], "publish_times": ["12:00"],
                    "version": 1,
                }, known_account_ids=[101])
            self.assertEqual(caught.exception.code, "x_account_not_found")

    def test_save_endpoint_validates_identity_without_filtering_restrictions(self):
        from features.x_accounts import oauth_service as oauth
        rows = [{"id": 101, "publish_eligible": True},
                {"id": 102, "publish_eligible": False}]
        connection = mock.Mock()
        connection.execute.return_value.fetchall.return_value = rows
        store = mock.Mock()
        store.save_schedule_config.return_value = {"account_ids": [101, 102]}
        for source in ("material", "drama"):
            with self.subTest(source=source), \
                 mock.patch.object(oauth, "_post_page_actor", return_value={}), \
                 mock.patch.object(oauth, "db_connect", return_value=connection), \
                 mock.patch.object(oauth, "row_to_item", side_effect=lambda r: r), \
                 mock.patch.object(oauth, "_x_posts_api", return_value=(XPostError, lambda _: store, None)):
                oauth.save_post_schedule_request({"settings": {"account_ids": [101, 102]}},
                                                 source, "xPosts")
            self.assertEqual(store.save_schedule_config.call_args.kwargs,
                             {"known_account_ids": [101, 102]})
            store.schedule_account_blockers.assert_not_called()


if __name__ == "__main__":
    unittest.main()
