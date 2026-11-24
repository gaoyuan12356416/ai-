import base64
from contextlib import nullcontext
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("codex_account_watch", Path(__file__).with_name("codex_account_watch.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def auth(account):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "user-" + account}).encode()).decode().rstrip("=")
    return {"auth_mode": "chatgpt", "tokens": {"account_id": account, "id_token": "test." + payload + ".signature"}}


def limits(used, count=0, credits=None):
    result = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": used, "windowDurationMins": 10080,
                                                               "resetsAt": 2000000000}},
              "rateLimitResetCredits": {"availableCount": count, "credits": credits}}
    return result


class FakeServer:
    def __init__(self, owner, home):
        self.owner = owner
        self.home = home
        self.account = m.read_json(home / "auth.json")["tokens"]["account_id"]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def call(self, method, params=None):
        if method == "account/read":
            return {"account": {"type": "chatgpt", "planType": "pro"}}
        if method == "account/rateLimits/read":
            self.owner.usage_accounts.append(self.account)
            if self.account == "original":
                return self.owner.original_limits
            if len(self.owner.readbacks) > 1:
                return self.owner.readbacks.pop(0)
            return self.owner.readbacks[0]
        if method == "account/rateLimitResetCredit/consume":
            if self.account != "new":
                raise AssertionError("attempted to redeem an original-account credit")
            self.owner.reset_homes.append(self.home)
            self.owner.reset_calls.append(dict(params))
            result = self.owner.reset_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return {"outcome": result}
        raise AssertionError(method)


class AccountWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "accounts"
        self.active = Path(self.temp.name) / "active"
        self.readbacks = [limits(15)]
        self.original_limits = limits(50, 2)
        self.usage_accounts = []
        self.reset_homes = []
        self.reset_calls = []
        self.reset_results = []
        for account in ("original", "new"):
            m.atomic_json(self.root / "accounts" / account / "home/auth.json", auth(account))
        m.atomic_json(self.active / "auth.json", auth("new"))
        m.atomic_json(self.root / "state.json", {"active": "new", "phase": "watching", "thresholdPercent": 1,
                                                "identities": {k: m.auth_identity(auth(k)) for k in ("original", "new")}})
        self.guard = patch.object(m, "credential_switch_window", lambda home: nullcontext())
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.print = patch("sys.stdout", new_callable=io.StringIO)
        self.print.start()
        self.addCleanup(self.print.stop)

    def watch(self):
        return m.Watch(self.root, self.active, lambda home: FakeServer(self, home))

    def start_on_original(self):
        m.atomic_json(self.active / "auth.json", auth("original"))
        watch = self.watch()
        watch.state.update(active="original", phase="fallback_complete")
        watch.save()
        return watch

    def test_secondary_window_can_trigger_one_percent(self):
        result = limits(15)
        result["rateLimits"]["secondary"] = {"usedPercent": 99}
        self.assertEqual(m.summarize_usage(result)["remainingPercent"], 1)

    def test_unknown_usage_is_not_zero(self):
        self.assertIsNone(m.summarize_usage({})["remainingPercent"])
        self.assertIsNone(m.summarize_usage(limits(float("nan")))["remainingPercent"])

    def test_healthy_account_does_not_spend_reset_card(self):
        self.readbacks = [limits(15, 3)]
        watch = self.watch()
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        self.assertEqual(self.reset_calls, [])

    def test_renews_expiring_credential_in_its_registered_home(self):
        watch = self.watch()
        home = watch.home("original")
        value = auth("original")
        payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 300}).encode()).decode()
        value["tokens"]["access_token"] = "test." + payload + ".signature"
        m.atomic_json(home / "auth.json", value)
        calls = []
        real_call = FakeServer.call
        def record(server, method, params=None):
            if method == "account/read":
                calls.append((server.home, params["refreshToken"]))
            return real_call(server, method, params)
        with patch.object(FakeServer, "call", record):
            watch.usage(home, watch.expected("original"))
            watch.usage(self.active, watch.expected("new"))
        self.assertEqual(calls, [(home, True), (self.active, False)])

    def test_exactly_one_percent_stays_on_new(self):
        self.readbacks = [limits(99, 3)]
        watch = self.watch()
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        self.assertEqual(self.usage_accounts, ["new"])
        self.assertEqual(self.reset_calls, [])

    def test_fallback_then_reset_from_original_uses_new_accounts_expiring_card(self):
        credits = [{"id": key, "status": "available", "resetType": "codexRateLimits", "expiresAt": expiry}
                   for key, expiry in (("later", 4000000000), ("earlier", 3000000000))]
        self.readbacks = [limits(100, 2, credits)]
        self.original_limits = limits(99, 4)
        watch = self.watch()
        watch.tick()
        self.assertEqual(watch.state["active"], "original")
        self.assertEqual(self.reset_calls, [])
        self.readbacks = [limits(100, 2, credits), limits(0, 1)]
        self.original_limits = limits(100, 4)
        self.reset_results = ["reset"]
        watch = self.watch()
        watch.tick()
        self.assertEqual(self.reset_homes, [watch.home("new")])
        self.assertEqual(self.reset_calls[0]["creditId"], "earlier")
        self.assertEqual(watch.state["lastEvent"]["reason"], "reset_verified")
        self.assertNotIn("pendingReset", watch.state)
        self.assertEqual(watch.state["active"], "new")
        self.assertEqual(m.auth_identity(m.read_json(self.active / "auth.json")), m.auth_identity(auth("new")))

    def test_below_one_uses_even_fractional_original_allowance(self):
        self.readbacks = [limits(99.25, 0)]
        self.original_limits = limits(99.75, 5)
        watch = self.watch()
        watch.tick()
        self.assertEqual(watch.state["active"], "original")
        self.assertEqual(watch.state["phase"], "fallback_complete")
        self.assertEqual(m.auth_identity(m.read_json(self.active / "auth.json")), m.auth_identity(auth("original")))
        self.assertEqual(self.reset_calls, [])
        watch.tick()
        self.assertEqual(self.reset_calls, [])
        self.assertEqual(self.usage_accounts, ["new", "original", "new", "original"])

    def test_unknown_credit_count_still_uses_original_quota(self):
        self.readbacks = [limits(100, None)]
        watch = self.watch()
        watch.tick()
        self.assertEqual(watch.state["active"], "original")
        self.assertEqual(self.reset_calls, [])

    def test_original_empty_uses_recovered_new_quota_without_a_card(self):
        watch = self.start_on_original()
        self.original_limits = limits(100, 5)
        self.readbacks = [limits(99.5, 3)]
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        self.assertEqual(self.reset_calls, [])

    def test_either_usage_unknown_is_not_exhaustion(self):
        watch = self.start_on_original()
        self.original_limits = {}
        self.readbacks = [limits(100, 3)]
        watch.tick()
        self.assertEqual(self.usage_accounts, ["new", "original"])
        self.assertEqual(watch.state["lastEvent"]["account"], "original")
        self.original_limits = limits(100, 5)
        self.readbacks = [{}]
        watch.tick()
        self.assertEqual(watch.state["lastEvent"]["account"], "new")
        self.assertEqual(watch.state["active"], "original")
        self.assertEqual(self.reset_calls, [])

    def test_first_positive_primary_check_leaves_healthy_fallback(self):
        watch = self.start_on_original()
        self.original_limits = limits(10)
        self.readbacks = [limits(100), limits(99)]
        watch.tick()
        self.assertEqual(watch.state["active"], "original")
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        self.assertEqual(self.usage_accounts, ["new", "original", "new"])
        self.assertEqual(self.reset_calls, [])

    def test_both_empty_with_zero_or_unknown_new_cards_waits(self):
        watch = self.start_on_original()
        self.original_limits = limits(100, 5)
        for count, event in [(0, "both_exhausted_no_credit"), (None, "reset_credit_count_unknown")]:
            self.readbacks = [limits(100, count)]
            watch.tick()
            self.assertEqual(watch.state["lastEvent"]["event"], event)
            self.assertEqual(watch.state["active"], "original")
        self.assertEqual(self.reset_calls, [])

    def test_timeout_keeps_idempotency_key_after_restart(self):
        self.original_limits = limits(100, 5)
        self.readbacks = [limits(100, 1)]
        self.reset_results = [TimeoutError("uncertain response")]
        watch = self.watch()
        with self.assertRaises(TimeoutError):
            watch.tick()
        first = self.reset_calls[0]
        self.readbacks = [limits(100, 0), limits(0, 0)]
        self.reset_results = ["alreadyRedeemed"]
        restarted = self.watch()
        restarted.tick()
        self.assertEqual(self.reset_calls[1], first)
        self.assertNotIn("pendingReset", restarted.state)
        self.assertEqual(restarted.state["active"], "new")

    def test_unconfirmed_request_survives_fallback_and_is_only_replayed_when_both_empty(self):
        self.original_limits = limits(100)
        self.readbacks = [limits(100, 1)]
        self.reset_results = [TimeoutError("uncertain response")]
        with self.assertRaises(TimeoutError):
            self.watch().tick()
        original_request = self.reset_calls[0]
        self.original_limits = limits(10)
        watch = self.watch()
        watch.tick()
        self.assertEqual(watch.state["active"], "original")
        self.assertEqual(watch.state["pendingReset"]["idempotencyKey"], original_request["idempotencyKey"])
        watch.tick()
        self.assertEqual(len(self.reset_calls), 1)
        self.original_limits = limits(100)
        self.readbacks = [limits(100, 0), limits(0, 0)]
        self.reset_results = ["alreadyRedeemed"]
        watch.tick()
        self.assertEqual(self.reset_calls, [original_request, original_request])
        self.assertEqual(watch.state["active"], "new")

    def test_success_waits_for_readback_and_never_spends_another_card(self):
        self.original_limits = limits(100)
        self.readbacks = [limits(100, 2), limits(100, 1)]
        self.reset_results = ["reset"]
        watch = self.start_on_original()
        watch.tick()
        watch.tick()
        self.assertEqual(len(self.reset_calls), 1)
        self.assertEqual(watch.state["lastEvent"]["event"], "awaiting_reset_readback")
        self.assertEqual(watch.state["active"], "original")
        self.readbacks = [limits(99, 1)]
        self.original_limits = limits(60)
        watch.tick()
        self.assertEqual(len(self.reset_calls), 1)
        self.assertEqual(watch.state["active"], "new")

    def test_lost_receipt_with_observed_recovery_reconciles_before_next_credit(self):
        watch = self.start_on_original()
        self.original_limits = limits(100)
        self.readbacks = [limits(100, 2)]
        self.reset_results = [TimeoutError("reply lost")]
        with self.assertRaises(TimeoutError):
            watch.tick()
        first = self.reset_calls[0]
        self.readbacks = [limits(0, 1)]
        watch = self.watch()
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        self.assertTrue(watch.state["pendingReset"]["recoveryObserved"])
        self.assertEqual(self.reset_calls, [first])
        self.readbacks = [limits(100, 1)]
        self.reset_results = ["alreadyRedeemed"]
        watch.tick()
        self.assertEqual(self.reset_calls, [first, first])
        watch.tick()
        self.assertNotIn("pendingReset", watch.state)
        self.readbacks = [limits(100, 1), limits(0, 0)]
        self.reset_results = ["reset"]
        watch.tick()
        self.assertNotEqual(self.reset_calls[-1]["idempotencyKey"], first["idempotencyKey"])
        self.assertEqual(len(self.reset_calls), 3)

    def test_nothing_to_reset_waits_for_quota_change(self):
        self.original_limits = limits(100)
        self.readbacks = [limits(100, 1)]
        self.reset_results = ["nothingToReset", "reset"]
        watch = self.watch()
        watch.tick()
        watch.tick()
        self.assertEqual(len(self.reset_calls), 1)
        self.readbacks = [limits(100, 1), limits(0, 0)]
        self.readbacks[0]["rateLimits"]["primary"]["resetsAt"] += 600
        watch.tick()
        self.assertEqual(len(self.reset_calls), 2)
        self.assertNotEqual(self.reset_calls[0]["idempotencyKey"], self.reset_calls[1]["idempotencyKey"])
        self.assertEqual(watch.state["lastEvent"]["event"], "reset_verified")

    def test_official_no_credit_keeps_current_account_and_detects_a_new_credit(self):
        self.readbacks = [limits(100, 1)]
        self.original_limits = limits(100, 2)
        self.reset_results = ["noCredit"]
        watch = self.start_on_original()
        watch.tick()
        watch.tick()
        self.assertEqual(watch.state["active"], "original")
        self.assertEqual(len(self.reset_calls), 1)
        self.readbacks = [limits(100, 0)]
        watch.tick()
        self.readbacks = [limits(100, 1), limits(0, 0)]
        self.reset_results = ["reset"]
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        self.assertEqual(len(self.reset_calls), 2)

    def test_unusable_original_is_not_installed(self):
        self.readbacks = [limits(100, 1)]
        self.original_limits = {}
        watch = self.watch()
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        self.assertEqual(watch.state["lastEvent"]["event"], "usage_unknown")
        self.assertEqual(self.reset_calls, [])

    def test_interrupted_reset_handoff_retries_only_the_credential_switch(self):
        watch = self.start_on_original()
        self.original_limits = limits(100)
        self.readbacks = [limits(100, 1), limits(0, 0)]
        self.reset_results = ["reset"]
        with patch.object(m, "credential_switch_window", side_effect=m.Deferred("busy")):
            with self.assertRaises(m.Deferred):
                watch.tick()
        self.assertEqual(watch.state["active"], "original")
        self.assertEqual(watch.state["pendingReset"]["outcome"], "reset")
        watch = self.watch()
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        self.assertNotIn("pendingReset", watch.state)
        self.assertEqual(len(self.reset_calls), 1)

    def test_initial_handoff_saves_latest_original_and_preserves_both_logins(self):
        old = auth("original")
        old["last_refresh"] = "most-recent"
        m.atomic_json(self.active / "auth.json", old)
        watch = self.watch()
        watch.state.update(active="original", phase="waiting_new_login")
        watch.save()
        watch.tick()
        self.assertEqual(watch.state["active"], "new")
        self.assertEqual(m.read_json(self.root / "accounts/original/home/auth.json")["last_refresh"], "most-recent")
        self.assertTrue((self.root / "accounts/new/home/auth.json").exists())

    def test_accidental_same_account_login_keeps_original(self):
        m.atomic_json(self.root / "accounts/new/home/auth.json", auth("original"))
        m.atomic_json(self.active / "auth.json", auth("original"))
        watch = self.watch()
        watch.state.update(active="original", phase="waiting_new_login")
        watch.save()
        watch.tick()
        self.assertEqual(watch.state["active"], "original")
        self.assertEqual(watch.state["lastEvent"]["event"], "new_login_is_original_account")

    def test_registration_of_exhausted_new_account_can_still_reset_when_both_empty(self):
        watch = self.start_on_original()
        watch.state["phase"] = "waiting_new_login"
        watch.state["identities"].pop("new")
        watch.save()
        self.original_limits = limits(100, 3)
        self.readbacks = [limits(100, 1)]
        watch.tick()
        self.assertEqual(watch.state["active"], "original")
        self.assertEqual(self.reset_calls, [])
        self.readbacks = [limits(100, 1), limits(0, 0)]
        self.reset_results = ["reset"]
        self.watch().tick()
        self.assertEqual(self.watch().state["active"], "new")
        self.assertEqual(self.reset_homes, [watch.home("new")])

    def test_interrupted_switch_is_reconciled_from_actual_credential(self):
        watch = self.watch()
        watch.state.update(active="original", phase="waiting_new_login",
                           pendingSwitch={"from": "original", "to": "new"})
        watch.save()
        watch.reconcile_switch()
        self.assertEqual(watch.state["active"], "new")
        self.assertNotIn("pendingSwitch", watch.state)

    def test_busy_source_writers_defer_without_replacing_auth(self):
        self.readbacks = [limits(100, 2)]
        with patch.object(m, "credential_switch_window", side_effect=m.Deferred("busy")):
            watch = self.watch()
            with self.assertRaises(m.Deferred):
                watch.tick()
        self.assertEqual(m.auth_identity(m.read_json(self.active / "auth.json")), m.auth_identity(auth("new")))
        self.assertEqual(watch.state["active"], "new")


if __name__ == "__main__":
    unittest.main()
