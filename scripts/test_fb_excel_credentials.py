"""Offline personal-token and video-title contracts. Never contacts Meta."""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_fb_video_publish_core import Fixture
from features.fb_auto_posts.publisher import GraphResult, RequestsGraphTransport
from features.fb_auto_posts.repositories import PagePoolRepository, RepositoryError
from features.fb_video_publish.query_broker import _validate_sources


class CredentialRepositoryTests(unittest.TestCase):
    def mysql(self, rows):
        return SimpleNamespace(schema="kunlunads_dev", select=Mock(return_value=rows))

    def test_personal_credentials_join_internal_identity_and_dedupe(self):
        rows = [dict(page_id="10001", credential_owner_id="100", credential_id=str(i),
                     fb_user_id="900", page_access_token=token)
                for i, token in enumerate(["test-one", "test-one", "test-two"], 1)]
        mysql = self.mysql(rows)
        result = PagePoolRepository(mysql).eligible_credentials("10001", owner_user_id="100")
        self.assertEqual(["1", "3"], [c.credential_id for c in result])
        sql, params = mysql.select.call_args.args
        self.assertEqual(("10001", "100"), params)
        self.assertIn("CAST(p.fb_user_id AS BINARY)=CAST(f.facebookUserID AS BINARY)", sql)
        self.assertIn("f.user_id=%s", sql)
        self.assertIn("p.status<>1", sql)
        self.assertNotIn("test-one", repr(result))
        _validate_sources(sql)

    def test_wrong_owner_or_page_is_never_accepted(self):
        for changes in ({"credential_owner_id": "101"}, {"page_id": "10002"}):
            row = dict(page_id="10001", credential_owner_id="100", credential_id="1",
                       fb_user_id="900", page_access_token="test-only")
            row.update(changes)
            with self.subTest(changes=changes), self.assertRaises(RepositoryError):
                PagePoolRepository(self.mysql([row])).eligible_credentials("10001", owner_user_id="100")

    def test_empty_owner_never_falls_back_to_shared_query(self):
        for owner in ("", "0", "false", True):
            mysql = self.mysql([])
            with self.subTest(owner=owner), self.assertRaises(RepositoryError):
                PagePoolRepository(mysql).eligible_credentials("10001", owner_user_id=owner)
            mysql.select.assert_not_called()

    def test_counts_are_batched_and_contain_no_token_value(self):
        mysql = self.mysql([{"page_id": "10001", "credential_count": "2"}])
        result = PagePoolRepository(mysql).owner_credential_counts(["10001", "10002"], "100")
        self.assertEqual({"10001": 2, "10002": 0}, result)
        sql, params = mysql.select.call_args.args
        self.assertEqual(["100", "10001", "10002"], params)
        self.assertIn("COUNT(DISTINCT p.page_access_token)", sql)
        self.assertNotIn("accessToken", sql)
        _validate_sources(sql)

    def test_default_credential_query_keeps_legacy_scope(self):
        mysql = self.mysql([])
        self.assertEqual([], PagePoolRepository(mysql).eligible_credentials("10001"))
        sql, params = mysql.select.call_args.args
        self.assertNotIn("ads_facebook_info", sql)
        self.assertEqual(("10001",), params)


class PersonalWorkerTests(Fixture):
    def freeze_policy(self, **policy):
        self.submit()
        self.ready()
        with self.store.connect() as conn:
            task = conn.execute("SELECT task_id,snapshot_json FROM fb_video_task").fetchone()
            snap = json.loads(task["snapshot_json"])
            snap.update(policy)
            conn.execute("UPDATE fb_video_task SET snapshot_json=? WHERE task_id=?",
                         (json.dumps(snap, ensure_ascii=False), task["task_id"]))

    def scoped_worker(self, outcomes=None, personal_count=2):
        worker = self.worker(outcomes)
        candidates = [SimpleNamespace(credential_id=str(i), fb_user_id="900", token="test-only")
                      for i in range(personal_count)]
        worker.pages = SimpleNamespace(eligible_credentials=Mock(return_value=candidates))
        return worker

    def test_personal_scope_is_frozen_and_used_for_execute_and_reconcile(self):
        self.freeze_policy(credential_mode="operator", credential_owner_id="100")
        worker = self.scoped_worker()
        self.assertEqual("submitted", worker.execute_next()["status"])
        worker.pages.eligible_credentials.assert_called_once_with("11", owner_user_id="100")
        self.now += timedelta(seconds=61)
        self.assertEqual("published", worker.reconcile_next()["status"])
        self.assertEqual([(("11",), {"owner_user_id": "100"})] * 2,
                         [(c.args, c.kwargs) for c in worker.pages.eligible_credentials.call_args_list])

    def test_missing_personal_authorization_never_retries_shared(self):
        self.freeze_policy(credential_mode="operator", credential_owner_id="100")
        worker = self.scoped_worker(personal_count=0)
        self.assertEqual("failed", worker.execute_next()["status"])
        self.assertEqual("operator_authorization_unavailable", self.tasks()[0]["error_code"])
        self.assertEqual([], worker.calls)
        worker.pages.eligible_credentials.assert_called_once_with("11", owner_user_id="100")

    def test_invalid_personal_policy_fails_closed_without_credential_query(self):
        for mode, owner in (("operator", ""), ("operator", "101"), ("unsupported", "100")):
            with self.subTest(mode=mode, owner=owner):
                if self.tasks():
                    self.setUp()
                self.freeze_policy(credential_mode=mode, credential_owner_id=owner)
                worker = self.scoped_worker()
                self.assertEqual("failed", worker.execute_next()["status"])
                worker.pages.eligible_credentials.assert_not_called()
                self.assertEqual([], worker.calls)

    def test_personal_scope_rotates_only_until_unknown(self):
        self.freeze_policy(credential_mode="operator", credential_owner_id="100")
        worker = self.scoped_worker([GraphResult("definite_failure", error_code="fb_graph_368"),
                                     GraphResult("unknown")], personal_count=3)
        self.assertEqual("unknown", worker.execute_next()["status"])
        self.assertEqual(2, len(worker.calls))
        worker.pages.eligible_credentials.assert_called_once_with("11", owner_user_id="100")

    def test_personal_authorization_revoked_before_reconcile_stays_unresolved(self):
        self.freeze_policy(credential_mode="operator", credential_owner_id="100")
        worker = self.scoped_worker()
        worker.execute_next()
        worker.pages.eligible_credentials.return_value = []
        self.now += timedelta(seconds=61)
        result = worker.reconcile_next()
        self.assertNotEqual("published", result["status"])
        self.assertEqual(1, len(worker.calls))
        worker.pages.eligible_credentials.assert_called_with("11", owner_user_id="100")

    def test_old_snapshot_retains_shared_scope(self):
        self.freeze_policy()
        worker = self.scoped_worker()
        worker.execute_next()
        worker.pages.eligible_credentials.assert_called_once_with("11")

    def test_title_is_passed_independently(self):
        self.freeze_policy(title="视频标题", credential_mode="operator", credential_owner_id="100")
        worker = self.scoped_worker()
        publish = Mock(return_value=GraphResult("success", post_id="11_123"))
        worker.graph.publish_video = publish
        worker.execute_next()
        self.assertEqual({"title": "视频标题"}, publish.call_args.kwargs)
        self.assertIn("测试剧", publish.call_args.args[3])


class GraphTitleTests(unittest.TestCase):
    def test_graph_title_is_optional_and_legacy_calls_omit_it(self):
        credential = SimpleNamespace(token="offline-test")
        for title in ("", "本次标题"):
            session = Mock()
            session.post.return_value = SimpleNamespace(headers={}, status_code=200, json=lambda: {"id": "123"})
            with patch("features.fb_auto_posts.publisher.requests.Session", return_value=session):
                result = RequestsGraphTransport().publish_video("10001", credential, "https://example.test/a.mp4", "文案", title=title)
            self.assertEqual("success", result.kind)
            data = session.post.call_args.kwargs["data"]
            self.assertEqual("文案", data["description"])
            self.assertEqual(bool(title), "title" in data)
            if title:
                self.assertEqual(title, data["title"])


if __name__ == "__main__":
    unittest.main()
