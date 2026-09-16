#!/usr/bin/env python3
"""Offline sidecar-ledger tests. Every account, credential and X call is fake."""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_accounts import client, youtube_shares as shares


class CallbackError(RuntimeError):
    def __init__(self, code, status=403, message="unsafe credential or upstream payload"):
        super().__init__(message)
        self.code, self.status = code, status


class YouTubeSharesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "youtube-shares" / "ledger.sqlite3"
        self.now = 10000.0
        self.owner = {"tenant_key": "tenant", "user_id": "owner", "role": "user"}
        self.other = {"tenant_key": "tenant", "user_id": "other", "role": "user"}
        self.admin = {"tenant_key": "tenant", "user_id": "admin", "role": "admin"}
        self.accounts = {
            index: {"id": index, "username": "test_%s" % index, "status": "active",
                    "publish_approved": True, "owner_tenant": "tenant", "owner_user": "owner",
                    "premium_subscriber": False, "subscription_type": "none", "drama_language": "ja"}
            for index in (1, 2, 3)
        }
        self.sent = []
        self.verifications = []
        self.credentials_open = 0
        self.service = self.new_service()
        self.addCleanup(self.service.stop_worker)

    def normalize_scope(self, actor, scope):
        if not isinstance(actor, dict) or not actor.get("tenant_key") or not actor.get("user_id"):
            raise CallbackError("invalid_request", 400)
        if scope not in {"mine", "all"}:
            raise CallbackError("invalid_request", 400)
        if scope == "all" and actor.get("role") != "admin":
            raise CallbackError("x_admin_required", 403)
        return dict(actor), scope

    def get_account(self, account_id, actor, scope):
        self.normalize_scope(actor, scope)
        account = self.accounts.get(account_id)
        if not account or (scope == "mine" and (
            account["owner_tenant"] != actor["tenant_key"] or account["owner_user"] != actor["user_id"]
        )):
            raise CallbackError("x_account_not_found", 404)
        return dict(account)

    def verify(self, account_id, actor, scope, **kwargs):
        self.verifications.append((account_id, kwargs))
        return self.get_account(account_id, actor, scope)

    @contextlib.contextmanager
    def credentials(self, account_id, actor, scope):
        self.credentials_open += 1
        try:
            yield self.get_account(account_id, actor, scope), "fake-access-token"
        finally:
            self.credentials_open -= 1

    def sender(self, token, text):
        self.assertEqual(token, "fake-access-token")
        self.assertEqual(self.credentials_open, 1, "send must hold credential context")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            attempt = conn.execute("SELECT status FROM youtube_share_attempt ORDER BY started_at DESC,item_id DESC LIMIT 1").fetchone()
            self.assertEqual(attempt, ("unknown_outcome",), "attempt must commit before sending")
        self.sent.append(text)
        return 201, {"data": {"id": str(1000 + len(self.sent))}}

    def new_service(self, **kwargs):
        return shares.YouTubeShares(
            self.db_path,
            normalize_scope=self.normalize_scope,
            get_account=self.get_account,
            verify_account=self.verify,
            publish_credentials=self.credentials,
            sender=kwargs.get("sender", self.sender),
            clock=lambda: self.now,
        )

    def payload(self, **updates):
        video_id = updates.get("video_id", "abcdefghijk")
        url = shares.canonical_url(video_id) if isinstance(video_id, str) and len(video_id) == 11 else "https://www.youtube.com/watch?v=abcdefghijk"
        result = {
            "actor": self.owner, "scope": "mine", "task_id": "youtube-task-1",
            "video_id": video_id, "youtube_url": url,
            "title": "Sample title", "text": "Sample title\n\n" + url,
            "description_template": "{title}\n\n{youtube_url}",
            "operation_id": str(uuid.uuid4()), "account_ids": [1], "source_verified_at": self.now,
        }
        result.update(updates)
        return result

    def read_run(self, run_id, **updates):
        payload = {"actor": self.owner, "scope": "mine", "task_id": "youtube-task-1", "run_id": run_id}
        payload.update(updates)
        return self.service.run(payload)["run"]

    def count(self, table):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def test_non_premium_account_publishes_link_inside_lock(self):
        run = self.service.create(self.payload())["run"]
        self.assertEqual(run["status"], "queued")
        self.assertTrue(self.service.process_next())
        result = self.read_run(run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items"][0]["status"], "published")
        self.assertEqual(result["items"][0]["post_url"], "https://x.com/i/status/1001")
        self.assertEqual(self.verifications, [(1, {"preserve_transient_status": True, "require_publish_approved": True})])
        self.assertFalse(self.service.process_next())

    def test_same_operation_replays_without_new_attempt_and_conflicts_on_body(self):
        payload = self.payload()
        first = self.service.create(payload)["run"]
        self.service.process_next()
        self.now += 10000
        second = self.service.create(payload)["run"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["items"][0]["status"], "published")
        with self.assertRaises(shares.YouTubeShareError) as error:
            self.service.create(dict(payload, text="changed\n" + payload["youtube_url"]))
        self.assertEqual(error.exception.code, "youtube_share_idempotency_conflict")
        self.assertEqual(self.count("youtube_share_run"), 1)
        self.assertEqual(self.count("youtube_share_attempt"), 1)

    def test_new_operation_duplicate_queued_then_published_reads_original(self):
        first = self.service.create(self.payload())["run"]
        duplicate = self.service.create(self.payload(text="different description\n" + shares.canonical_url("abcdefghijk")))["run"]
        self.assertNotEqual(first["id"], duplicate["id"])
        self.assertTrue(duplicate["items"][0]["duplicate"])
        self.assertEqual(duplicate["items"][0]["status"], "queued")
        self.service.process_next()
        final = self.read_run(duplicate["id"])
        self.assertEqual(final["items"][0]["post_url"], "https://x.com/i/status/1001")
        self.assertFalse(self.service.process_next())
        self.assertEqual(len(self.sent), 1)

    def test_concurrent_creates_enforce_operation_and_video_account_uniqueness(self):
        payload = self.payload()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            runs = list(pool.map(lambda _: self.service.create(dict(payload))["run"], range(8)))
        self.assertEqual(len({run["id"] for run in runs}), 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            duplicates = list(pool.map(lambda _: self.service.create(self.payload())["run"], range(8)))
        self.assertTrue(all(run["items"][0]["duplicate"] for run in duplicates))
        self.service.process_next()
        self.assertFalse(self.service.process_next())
        self.assertEqual(self.count("youtube_share_attempt"), 1)

    def test_concurrent_claim_returns_one_claim_and_claim_replay_cannot_send_twice(self):
        self.service.create(self.payload())
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.service.claim_next(), range(8)))
        claims = [item for item in claims if item]
        self.assertEqual(len(claims), 1)
        self.service.process_claim(claims[0])
        self.service.process_claim(claims[0])
        self.assertEqual(len(self.sent), 1)

    def test_timeout_is_unknown_and_new_operation_cannot_retry_it(self):
        run = self.service.create(self.payload())["run"]
        sender = mock.Mock(side_effect=TimeoutError("fake-access-token must never escape"))
        self.service.sender = sender
        self.service.process_next()
        result = self.read_run(run["id"])
        self.assertEqual(result["items"][0]["status"], "unknown_outcome")
        self.assertNotIn("fake-access-token", json.dumps(result))
        duplicate = self.service.create(self.payload())["run"]
        self.assertTrue(duplicate["items"][0]["duplicate"])
        self.assertFalse(self.service.process_next())
        sender.assert_called_once()

    def test_failure_preserves_audit_and_allows_new_manual_operation(self):
        payload = self.payload()
        first = self.service.create(payload)["run"]
        self.service.sender = mock.Mock(return_value=(403, {"secret": "ignore upstream details"}))
        self.service.process_next()
        self.assertEqual(self.read_run(first["id"])["items"][0]["status"], "failed")
        self.assertEqual(self.service.create(payload)["run"]["id"], first["id"])
        second = self.service.create(self.payload())["run"]
        self.assertFalse(second["items"][0]["duplicate"])
        self.service.sender = self.sender
        self.service.process_next()
        self.assertEqual(self.read_run(second["id"])["items"][0]["status"], "published")
        self.assertEqual(self.count("youtube_share_attempt"), 2)
        self.assertEqual(self.read_run(first["id"])["items"][0]["status"], "failed")

    def test_restart_marks_claim_unknown_and_only_untouched_queued_resumes(self):
        run = self.service.create(self.payload(account_ids=[1, 2]))["run"]
        claimed = self.service.claim_next()
        self.assertEqual(claimed["account_id"], 1)
        restarted = self.new_service()
        self.addCleanup(restarted.stop_worker)
        self.assertEqual(restarted.recover_interrupted(), 1)
        restarted.process_next()
        self.assertFalse(restarted.process_next())
        statuses = [item["status"] for item in self.read_run(run["id"])["items"]]
        self.assertEqual(statuses, ["unknown_outcome", "published"])
        self.assertEqual(len(self.sent), 1)

    def test_restart_after_durable_attempt_marker_never_reissues_post(self):
        run = self.service.create(self.payload())["run"]
        claimed = self.service.claim_next()
        self.assertTrue(self.service._begin_attempt(claimed))
        restarted = self.new_service()
        self.addCleanup(restarted.stop_worker)
        restarted.recover_interrupted()
        self.assertFalse(restarted.process_next())
        self.assertEqual(self.read_run(run["id"])["items"][0]["status"], "unknown_outcome")
        self.assertEqual(self.count("youtube_share_attempt"), 1)
        self.assertEqual(self.sent, [])

    def test_terminal_storage_error_recovers_same_process_without_second_http(self):
        run = self.service.create(self.payload())["run"]
        original_finish = self.service._finish
        finish_calls = []
        def transient_finish(*args, **kwargs):
            finish_calls.append(1)
            if len(finish_calls) <= 2:
                raise sqlite3.OperationalError("temporary write failure")
            return original_finish(*args, **kwargs)
        with mock.patch.object(self.service, "_finish", side_effect=transient_finish):
            self.assertTrue(self.service.process_next())
            self.assertEqual(self.read_run(run["id"])["items"][0]["status"], "publishing")
            self.assertFalse(self.service.process_next())
            self.assertFalse(self.service.process_next())
        result = self.read_run(run["id"])
        self.assertEqual(result["items"][0]["status"], "published")
        self.assertEqual(result["items"][0]["post_url"], "https://x.com/i/status/1001")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.count("youtube_share_attempt"), 1)
        self.assertFalse(self.service._terminal_pending)

    def test_disabled_or_unapproved_anywhere_rejects_complete_create(self):
        for change, code in (({"status": "disabled"}, "x_account_disabled"), ({"publish_approved": False}, "x_account_publish_not_approved")):
            with self.subTest(change=change):
                self.accounts[2].update(status="active", publish_approved=True)
                self.accounts[2].update(change)
                with self.assertRaises(shares.YouTubeShareError) as error:
                    self.service.create(self.payload(account_ids=[1, 2]))
                self.assertEqual(error.exception.code, code)
                self.assertEqual(self.count("youtube_share_run"), 0)

    def test_disabled_after_queue_fails_without_attempt(self):
        run = self.service.create(self.payload())["run"]
        self.accounts[1]["status"] = "disabled"
        self.service.process_next()
        result = self.read_run(run["id"])
        self.assertEqual(result["items"][0]["status"], "failed")
        self.assertEqual(self.count("youtube_share_attempt"), 0)

    def test_final_credential_recheck_rejects_approval_loss(self):
        run = self.service.create(self.payload())["run"]
        @contextlib.contextmanager
        def credentials(*_args):
            account = dict(self.accounts[1], publish_approved=False)
            yield account, "fake-access-token"
        self.service.publish_credentials = credentials
        self.service.process_next()
        self.assertEqual(self.read_run(run["id"])["items"][0]["status"], "failed")
        self.assertEqual(self.count("youtube_share_attempt"), 0)

    def test_scope_forbidden_cross_owner_tenant_and_task(self):
        with self.assertRaises(shares.YouTubeShareError) as error:
            self.service.create(self.payload(scope="all"))
        self.assertEqual(error.exception.status, 403)
        run = self.service.create(self.payload())["run"]
        for updates in ({"actor": self.other}, {"actor": dict(self.owner, tenant_key="elsewhere")}, {"task_id": "wrong-task"}):
            with self.subTest(updates=updates), self.assertRaises(shares.YouTubeShareError) as denied:
                self.read_run(run["id"], **updates)
            self.assertEqual(denied.exception.status, 403)
        self.assertEqual(self.read_run(run["id"], actor=self.admin, scope="all")["id"], run["id"])
        self.assertEqual(self.service.query({"actor": self.other, "task_id": "youtube-task-1", "video_id": "abcdefghijk"}), {"history": []})

    def test_history_and_run_recheck_account_ownership(self):
        run = self.service.create(self.payload())["run"]
        self.accounts[1]["owner_user"] = "other"
        with self.assertRaises(shares.YouTubeShareError):
            self.read_run(run["id"])
        with self.assertRaises(shares.YouTubeShareError):
            self.service.query({"actor": self.owner, "task_id": "youtube-task-1", "video_id": "abcdefghijk"})

    def test_operation_lookup_is_scoped_and_task_bound(self):
        payload = self.payload()
        run = self.service.create(payload)["run"]
        query = {"actor": self.owner, "task_id": "youtube-task-1", "operation_id": payload["operation_id"]}
        result = self.service.query(query)["run"]
        self.assertEqual(result["id"], run["id"])
        self.assertEqual(result["account_ids"], [1])
        self.assertEqual(result["description_template"], payload["description_template"])
        self.assertEqual(self.service.query(dict(query, actor=self.other)), {"run": None})
        with self.assertRaises(shares.YouTubeShareError) as error:
            self.service.query(dict(query, task_id="wrong-task"))
        self.assertEqual(error.exception.status, 403)

    def test_invalid_canonical_url_text_and_accounts_do_not_create_runs(self):
        invalid = [
            {"youtube_url": "https://youtu.be/abcdefghijk"}, {"video_id": "invalid"},
            {"text": "中" * 141 + "\n" + shares.canonical_url("abcdefghijk")},
            {"text": "a" * 281 + "\n" + shares.canonical_url("abcdefghijk")}, {"text": ""},
            {"text": "missing link"}, {"text": "\x00" + shares.canonical_url("abcdefghijk")},
            {"text": shares.canonical_url("abcdefghijk") + "extra"},
            {"text": shares.canonical_url("abcdefghijk") + "&untrusted=1"},
            {"account_ids": []}, {"account_ids": [1] * 21}, {"account_ids": [1, 1]},
            {"account_ids": [True]}, {"account_ids": ["1"]}, {"operation_id": "not-a-uuid"},
            {"source_verified_at": float("nan")}, {"source_verified_at": self.now + 31},
        ]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(shares.YouTubeShareError):
                self.service.create(self.payload(**change))
        self.assertEqual(self.count("youtube_share_run"), 0)

    def test_expired_source_create_and_delayed_queue_fail_before_send(self):
        with self.assertRaises(shares.YouTubeShareError) as error:
            self.service.create(self.payload(source_verified_at=self.now - 601))
        self.assertEqual(error.exception.code, "youtube_share_source_stale")
        run = self.service.create(self.payload())["run"]
        self.now += 601
        self.service.process_next()
        self.assertEqual(self.read_run(run["id"])["items"][0]["status"], "failed")
        self.assertEqual(self.verifications, [])
        self.assertEqual(self.count("youtube_share_attempt"), 0)

    def test_expiry_during_token_verification_fails_before_attempt(self):
        run = self.service.create(self.payload())["run"]
        def verify(*_args, **_kwargs):
            self.now += 601
            return dict(self.accounts[1])
        self.service.verify_account = verify
        self.service.process_next()
        self.assertEqual(self.read_run(run["id"])["items"][0]["status"], "failed")
        self.assertEqual(self.count("youtube_share_attempt"), 0)

    def test_partial_results_continue_independent_siblings(self):
        run = self.service.create(self.payload(account_ids=[1, 2, 3]))["run"]
        self.service.sender = mock.Mock(side_effect=[(201, {"data": {"id": "777"}}), TimeoutError("secret"), (429, {})])
        while self.service.process_next():
            pass
        result = self.read_run(run["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["status"] for item in result["items"]], ["published", "unknown_outcome", "failed"])
        self.assertEqual(self.count("youtube_share_attempt"), 3)

    def test_malformed_success_server_error_and_request_timeout_are_unknown(self):
        responses = [(201, {"data": {"id": "bad-id"}}), (200, {}), (500, {}), (408, {}), (302, {})]
        for index, response in enumerate(responses):
            with self.subTest(response=response):
                video_id = "abcdefghij" + str(index)
                run = self.service.create(self.payload(video_id=video_id, youtube_url=shares.canonical_url(video_id)))["run"]
                self.service.sender = mock.Mock(return_value=response)
                self.service.process_next()
                self.assertEqual(self.read_run(run["id"])["items"][0]["status"], "unknown_outcome")

    def test_preflight_error_code_is_safe_and_exception_text_is_discarded(self):
        run = self.service.create(self.payload())["run"]
        self.service.verify_account = mock.Mock(side_effect=CallbackError("x_token_revoked", 409, "Bearer actual-secret"))
        self.service.process_next()
        result = self.read_run(run["id"])
        self.assertNotIn("actual-secret", json.dumps(result))
        self.assertEqual(result["items"][0]["status"], "failed")
        self.assertEqual(self.count("youtube_share_attempt"), 0)

    def test_start_worker_is_singleton_and_stop_is_bounded(self):
        self.service.create(self.payload())
        entered, release = threading.Event(), threading.Event()
        def sender(*_args):
            entered.set()
            self.assertTrue(release.wait(2))
            return 201, {"data": {"id": "888"}}
        self.service.sender = sender
        self.service.start_worker()
        self.assertTrue(entered.wait(2))
        first = self.service._worker
        self.service.start_worker()
        self.assertIs(first, self.service._worker)
        release.set()
        self.service.stop_worker()
        self.assertFalse(first.is_alive())
        self.assertEqual(self.count("youtube_share_attempt"), 1)


class TransportAndWiringTests(unittest.TestCase):
    def test_transport_only_sends_text_to_fixed_x_endpoint_once(self):
        response = mock.MagicMock()
        response.getcode.return_value = 201
        response.read.return_value = b'{"data":{"id":"123"}}'
        response.__enter__.return_value = response
        with mock.patch.object(shares._POST_OPENER, "open", return_value=response) as opening:
            self.assertEqual(shares.send_text("fake-token", "hello"), (201, {"data": {"id": "123"}}))
        opening.assert_called_once()
        request = opening.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.x.com/2/tweets")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"text": "hello"})
        self.assertEqual(response.read.call_args.args, (shares.MAX_RESPONSE_BYTES + 1,))
        self.assertIsNone(shares._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))
        self.assertFalse(any(isinstance(handler, shares.urllib.request.ProxyHandler) and handler.proxies for handler in shares._POST_OPENER.handlers))

    def test_http_failure_status_preserved_and_malformed_body_discarded(self):
        error = urllib.error.HTTPError(shares.CREATE_TWEET_URL, 403, "unsafe body", {}, io.BytesIO(b"bad-json"))
        with mock.patch.object(shares._POST_OPENER, "open", side_effect=error) as opening:
            self.assertEqual(shares.send_text("fake-token", "hello"), (403, None))
        opening.assert_called_once()

    def test_client_uses_normalized_actor_and_fixed_backend_route(self):
        with mock.patch.object(client, "_request", return_value={"run": None}) as request:
            client.youtube_share_request("query", {"tenant_key": "t", "user_id": "u"}, operation_id="operation")
        self.assertEqual(request.call_args.args, ("/internal/youtube-shares/query",))
        self.assertEqual(request.call_args.kwargs["payload"]["scope"], "mine")
        self.assertEqual(request.call_args.kwargs["payload"]["actor"]["role"], "user")
        with self.assertRaises(client.XAccountsClientError):
            client.youtube_share_request("../posts/queue", {}, task_id="task")

    def test_routes_deny_daily_auto_nonloopback_and_accept_backend(self):
        # Local source import only. No serve(), storage initialization, token
        # read, live server import, or real socket/network request occurs.
        from features.x_accounts import oauth_service
        with mock.patch.multiple(oauth_service, INTERNAL_TOKEN="backend-test", DAILY_INTERNAL_TOKEN="daily-test", AUTO_INTERNAL_TOKEN="auto-test"):
            for action in ("query", "create", "run"):
                for token, address, allowed in (("backend-test", "127.0.0.1", True), ("daily-test", "127.0.0.1", False), ("auto-test", "127.0.0.1", False), ("backend-test", "192.0.2.10", False)):
                    with self.subTest(action=action, token=token, address=address):
                        handler = object.__new__(oauth_service.Handler)
                        handler.path = "/internal/youtube-shares/" + action
                        handler.client_address = (address, 1234)
                        handler.headers = {"Authorization": "Bearer " + token}
                        handler.send_json = mock.Mock()
                        handler.read_json = mock.Mock(return_value={"task_id": "task"})
                        with mock.patch.object(oauth_service, "youtube_share_request", return_value={"run": None}) as request:
                            handler.do_POST()
                        self.assertEqual(handler.send_json.call_args.args[0], (202 if action == "create" else 200) if allowed else 403)
                        self.assertEqual(request.call_count, 1 if allowed else 0)

    def test_storage_requires_mounted_data_disk_and_stays_under_it(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            storage = mount / "x-post-automation"
            storage.mkdir()
            with mock.patch.object(shares.os.path, "ismount", return_value=False):
                with self.assertRaises(shares.YouTubeShareError):
                    shares.ledger_path(storage, mount)
            with mock.patch.object(shares.os.path, "ismount", return_value=True):
                self.assertEqual(shares.ledger_path(storage, mount), storage / "youtube-shares" / "ledger.sqlite3")
                with self.assertRaises(shares.YouTubeShareError):
                    shares.ledger_path(mount.parent, mount)


if __name__ == "__main__":
    unittest.main(verbosity=2)
