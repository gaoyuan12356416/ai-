import importlib.util
import json
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("sync", Path(__file__).with_name("meta_asset_permission_sync.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class PolicyTests(unittest.TestCase):
    def test_status_unknown_not_normal(self):
        self.assertEqual(m.eligibility("page", {"is_published": True}, None)[0], "unknown")
        self.assertEqual(m.eligibility("page", {}, {"status": 0, "forbidden_status": 0})[0], "unknown")
        self.assertEqual(m.eligibility("account", {"account_status": 1}, {"account_status": 1, "is_inactive": 1})[0], "abnormal")
        self.assertEqual(m.eligibility("page", {"is_published": True}, {"status": 0, "forbidden_status": 1})[0], "abnormal")

    def test_full_control_and_permission_delta(self):
        self.assertFalse(m.full_access("account", ["ADVERTISE", "ANALYZE", "DRAFT"]))
        self.assertTrue(m.full_access("account", m.ACCOUNT_TASKS))
        self.assertFalse(m.full_access("page", m.PAGE_TASKS - {"MANAGE_LEADS"}))
        self.assertTrue(m.full_access("page", ["PROFILE_PLUS_FULL_CONTROL"]))
        self.assertEqual(m.expected_tasks("page", ["PROFILE_PLUS_ADVERTISE"]), {"PROFILE_PLUS_FULL_CONTROL"})

    def test_legacy_id_does_not_override_current_identity(self):
        graph = SimpleNamespace(get=lambda *a: {"id": "current", "name": "backup"})
        c = {"user_id": 916, "facebookUserID": "old"}
        identity = m.verify_identity(graph, c, {"current": {"id": "current", "name": "backup", "role": "EMPLOYEE"}})
        self.assertEqual(identity["meta_id"], "current")
        self.assertTrue(identity["stored_id_differs"])
        with self.assertRaises(ValueError):
            m.verify_identity(graph, c, {"old": {"name": "backup"}})

    def test_pagination_failure_never_returns_partial_list(self):
        graph = object.__new__(m.Graph)
        replies = iter([
            {"ok": True, "data": {"data": [{"id": "1"}], "paging": {"next": "redacted", "cursors": {"after": "abc"}}}},
            {"ok": False, "http": 400, "error": {"code": 190}}])
        graph.call = lambda *a: next(replies)
        with patch.object(m.time, "sleep"), self.assertRaises(m.GraphError):
            graph.rows("me/accounts", {}, "id")

    def test_looping_cursor_rejected(self):
        graph = object.__new__(m.Graph)
        graph.call = lambda *a: {"ok": True, "data": {"data": [], "paging": {"next": "n", "cursors": {"after": "a"}}}}
        with patch.object(m.time, "sleep"), self.assertRaises(ValueError):
            graph.rows("me/accounts", {}, "id")

    def test_rotation_and_both_intervals(self):
        clock = [0.0]
        r = m.Rotation([{"id": 1}, {"id": 2}], clock=lambda: clock[0], sleep=lambda n: clock.__setitem__(0, clock[0] + n))
        a = r.next(set()); r.before_write(a)
        b = r.next(set()); r.before_write(b)
        self.assertEqual(clock[0], 5)
        self.assertEqual(r.next(set()), a)
        r.before_write(a)
        self.assertEqual(clock[0], 30)
        r.disabled.add(1)
        self.assertEqual(r.next(set()), b)
        self.assertIsNone(r.next({2}))

    def test_journal_unknown_and_digest(self):
        with tempfile.TemporaryDirectory() as d:
            j = m.Journal(Path(d), "digest")
            j.append("intent", key="page:1")
            self.assertEqual(m.Journal(Path(d), "digest").unresolved(), {"page:1"})
            with self.assertRaises(ValueError):
                m.Journal(Path(d), "wrong")

    def test_page_assignment_uses_business_user_edge(self):
        calls = []
        def rows(*a, **kw):
            calls.append((a, kw))
            return {"123": {"id": "123", "tasks": sorted(m.PAGE_TASKS)}}
        assignment = m.read_assignment(SimpleNamespace(rows=rows), {}, {"kind": "page", "asset_id": "123"}, "target", "bm")
        self.assertTrue(m.full_access("page", assignment["tasks"]))
        self.assertEqual(calls[0][0][0], "target/assigned_pages")
        self.assertEqual(json.loads(calls[0][0][3]["pages"]), [123])

    def test_page_id_filter_must_be_honored(self):
        graph = SimpleNamespace(rows=lambda *a, **kw: {"999": {"id": "999"}})
        with self.assertRaises(ValueError):
            m.filtered_pages(graph, "user", {}, ["123"], "bm")

    def test_page_owner_rechecked_without_full_list_scan(self):
        graph = SimpleNamespace(rows=lambda *a, **kw: self.fail("unexpected ownership list scan"))
        asset = {"asset_id": "123"}
        partial = {"tasks": ["ADVERTISE"]}
        self.assertTrue(m.page_delegable_now(graph, {}, asset, partial, {"business": {"id": "bm"}}, "bm"))
        self.assertFalse(m.page_delegable_now(graph, {}, asset, partial, {"business": {"id": "other"}}, "bm"))
        self.assertTrue(m.page_delegable_now(graph, {}, asset, {"tasks": sorted(m.PAGE_TASKS)}, {}, "bm"))

    def test_visibility_recheck_never_retries_an_intended_write(self):
        with tempfile.TemporaryDirectory() as d:
            j = m.Journal(Path(d), "digest")
            result = {"status": "skipped", "reason": "source_permission_removed"}
            j.append("result", key="page:1", result=result)
            j.append("intent", key="page:2")
            j.append("result", key="page:2", result=result)
            self.assertEqual(m.retryable_unwritten_results(j), {"page:1"})

    def test_status_batch_is_get_only_and_preserves_unknowns(self):
        graph = object.__new__(m.Graph)
        graph.version = "v26.0"
        submitted = []
        def post(url, **kw):
            submitted.extend(json.loads(kw["data"]["batch"]))
            return SimpleNamespace(status_code=200, json=lambda: [
                {"code": 200, "body": json.dumps({"id": "123", "is_published": True})},
                {"code": 400, "body": json.dumps({"error": {"code": 100}})}])
        graph.session = SimpleNamespace(post=post)
        with patch.object(m.time, "sleep"), patch.object(m, "emit"):
            result = graph.batch_get(["123", "456"], "id,is_published", {"accessToken": "fake"})
        self.assertTrue(all(x["method"] == "GET" for x in submitted))
        self.assertTrue(result["123"]["is_published"])
        self.assertNotIn("is_published", result["456"])
        self.assertEqual(result["456"]["read_error"]["code"], 100)

    def test_status_batch_restriction_stops_reads(self):
        graph = object.__new__(m.Graph)
        graph.version = "v26.0"
        graph.session = SimpleNamespace(post=lambda *a, **kw: SimpleNamespace(status_code=200,
            json=lambda: [{"code": 400, "body": json.dumps({"error": {"code": 368}})}]))
        with self.assertRaises(m.GraphError):
            graph.batch_get(["123"], "id", {"accessToken": "fake"})

    def test_scoped_reads_are_get_only_and_resume_complete_items(self):
        graph = object.__new__(m.Graph); graph.version = "v26.0"
        calls = []
        class Client:
            def __init__(self, version): pass
            def call(self, method, key, credential, params):
                calls.append((method, key))
                return {"ok": True, "http": 200, "data": {"id": key, "user_tasks": ["ANALYZE"]}}
        with tempfile.TemporaryDirectory() as d, patch.object(m, "Graph", Client), patch.object(m, "emit"):
            checkpoint = Path(d) / "partial.json"
            credential = {"id": 1, "user_id": 1, "accessToken": "fake"}
            first = graph.concurrent_get(["1", "2"], "id,user_tasks", credential, checkpoint, workers=2)
            second = graph.concurrent_get(["1", "2", "3"], "id,user_tasks", credential, checkpoint, workers=2)
        self.assertEqual(set(first), {"1", "2"})
        self.assertEqual(set(second), {"1", "2", "3"})
        self.assertEqual(sorted(calls), [("GET", "1"), ("GET", "2"), ("GET", "3")])

    def test_scoped_read_restriction_prevents_new_submissions(self):
        graph = object.__new__(m.Graph); graph.version = "v26.0"
        calls = []
        class Client:
            def __init__(self, version): pass
            def call(self, method, key, credential, params):
                calls.append(key)
                return {"ok": False, "http": 400, "error": {"code": 368}}
        with tempfile.TemporaryDirectory() as d, patch.object(m, "Graph", Client), self.assertRaises(m.GraphError):
            graph.concurrent_get(["1", "2", "3"], "id", {"id": 1, "user_id": 1, "accessToken": "fake"}, Path(d)/"partial.json", workers=1)
        self.assertEqual(calls, ["1"])


class ExecuteTests(unittest.TestCase):
    def run_scenario(self, response, resume=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name)
        assets = [{"kind": "account", "asset_id": str(i), "category": "insufficient",
                   "desired_tasks": sorted(m.ACCOUNT_TASKS), "before_tasks": ["ADVERTISE"]} for i in (1, 2)]
        manifest = {"complete": True, "business_id": m.DEFAULT_BM, "code_version": "preview-version",
                    "identities": {"source": {"local_user_id": 803, "meta_id": "s"},
                                   "target": {"local_user_id": 916, "meta_id": "t"}}, "assets": assets}
        m.write_json(path / "manifest.json", manifest)
        digest = hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest()
        (path / "manifest.sha256").write_text(digest)
        if resume:
            journal = m.Journal(path, digest)
            journal.append("intent", key="account:1")
        credentials = [{"id": i, "user_id": u, "accessToken": "fake" + str(i), "facebookUserID": "old"}
                       for i, u in [(1, 803), (2, 916), (3, 1), (4, 2)]]
        posts = []
        class G:
            def rows(self, *a, **kw): return {}
            def call(self, *a, **kw):
                posts.append(a)
                return response
        args = SimpleNamespace(run_dir=str(path), business=m.DEFAULT_BM, command="resume" if resume else "execute", admin_cache="unused", code_version="execute-version")
        db = SimpleNamespace(assets=lambda kind, ids: {m.normalized_id(ids[0]): {"account_status": 1, "is_inactive": 0}})
        identity = lambda graph, c, users: {"meta_id": "s" if c["user_id"] == 803 else "t"}
        patches = [patch.object(m, "primary_admin", return_value=(credentials[2], {})),
                   patch.object(m, "verify_admin", return_value={"proof": "ADMIN"}),
                   patch.object(m, "verify_identity", side_effect=identity),
                   patch.object(m, "discover_admins", return_value=credentials[2:]),
                   patch.object(m, "read_assignment", return_value={"tasks": ["ADVERTISE"]}),
                   patch.object(m, "live_asset", return_value={"account_status": 1, "user_tasks": sorted(m.ACCOUNT_TASKS)}),
                   patch.object(m, "verify_after", return_value={"status": "pending_verification", "reason": "not_confirmed"}),
                   patch.object(m.time, "sleep"), patch.object(m, "emit")]
        for p in patches: p.start()
        try:
            m.execute(args, db, G(), credentials)
        finally:
            for p in reversed(patches): p.stop()
        return posts, json.loads((path / "execution_summary.json").read_text()), m.Journal(path, digest)

    def test_368_pauses_bm_without_switching_token(self):
        posts, summary, journal = self.run_scenario({"ok": False, "http": 400, "error": {"code": 368, "error_subcode": 1390008}})
        self.assertEqual(len(posts), 1)
        self.assertTrue(summary["bm_paused"])
        self.assertEqual(summary["unattempted"], 1)
        self.assertEqual(journal.results()["account:1"]["status"], "restricted")

    def test_ambiguous_write_has_no_automatic_retry(self):
        posts, summary, journal = self.run_scenario({"ok": False, "http": 599, "transport_error": "Timeout"})
        self.assertEqual(len(posts), 1)
        self.assertEqual(journal.results()["account:1"]["status"], "unknown")
        self.assertEqual(summary["unattempted"], 1)

    def test_resume_unknown_item_does_not_write_it(self):
        posts, _, _ = self.run_scenario({"ok": False, "http": 400, "error": {"code": 100}}, resume=True)
        self.assertEqual(posts, [])  # The unresolved canary also pauses the rest of its class.

    def test_429_does_not_rotate_around_limit(self):
        posts, summary, _ = self.run_scenario({"ok": False, "http": 429, "retry_after": "300"})
        self.assertEqual(len(posts), 1)
        self.assertTrue(summary["bm_paused"])


if __name__ == "__main__":
    unittest.main()
