import copy
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.ad_control_v3 import routes
from features.ad_control_v3.errors import AdControlV3Error
from features.ad_control_v3.live_execution import FacebookLiveExecutor
from features.ad_control_v3.repository import MemoryRepository
from features.ad_control_v3.time_utils import (
    convert_audit_times,
    copied_object_name,
    utc8_business_date,
    utc8_copy_suffix,
    utc8_date_bounds,
    utc8_iso_text,
)
from tests.test_ad_control_v3_core import NORMAL, make_service


FIXED_UTC = datetime(2026, 7, 17, 6, 55, tzinfo=timezone.utc)
EXPECTED_SUFFIX = "[*copybyAI*07171455]"


class NamingClient:
    def __init__(self):
        self.write_count = 0
        self.posts = []
        self.names = {}

    def post(self, object_id, values):
        self.write_count += 1
        self.posts.append((object_id, copy.deepcopy(values)))
        if object_id == "source-c/copies":
            return {"copied_campaign_id": "new-c"}
        if object_id == "source-set/copies":
            return {"copied_adset_id": "new-set"}
        if object_id == "source-ad/copies":
            return {"copied_ad_id": "new-ad"}
        if "name" in values:
            self.names[object_id] = values["name"]
        return {"success": True}

    def get(self, object_id, fields):
        common = {
            "id": object_id,
            "name": self.names.get(object_id, "Meta default copy name"),
            "account_id": "123",
            "status": "PAUSED",
            "configured_status": "PAUSED",
            "effective_status": "PAUSED",
        }
        if object_id == "new-c":
            return dict(common, source_campaign_id="source-c")
        if object_id == "new-set":
            return dict(common, source_adset_id="source-set", campaign_id="new-c")
        if object_id == "new-ad":
            return dict(common, source_ad_id="source-ad", adset_id="new-set", creative={"id": "creative-1"})
        raise AssertionError("unexpected readback: %s" % object_id)


class CopyNamePrimitiveTests(unittest.TestCase):
    def test_suffix_uses_fixed_utc8_month_day_hour_minute(self):
        self.assertEqual(EXPECTED_SUFFIX, utc8_copy_suffix(FIXED_UTC))

    def test_long_source_name_is_truncated_without_truncating_suffix(self):
        name = copied_object_name("剧" * 400, EXPECTED_SUFFIX)
        self.assertEqual(255, len(name))
        self.assertTrue(name.endswith(EXPECTED_SUFFIX))
        self.assertEqual("AI Copy" + EXPECTED_SUFFIX, copied_object_name("", EXPECTED_SUFFIX))

    def test_campaign_adset_and_ad_are_renamed_while_paused(self):
        client = NamingClient()
        executor = FacebookLiveExecutor(lambda: None, lambda: None, lambda: None, clock=lambda: FIXED_UTC)
        campaign = executor._copy_campaign(
            client, "source-c", source_name="Campaign A", copy_suffix=EXPECTED_SUFFIX
        )
        adset = executor._copy_adset(
            client, "source-set", "new-c", source_name="Ad Set A", copy_suffix=EXPECTED_SUFFIX
        )
        ad = executor._copy_ad(
            client,
            {"id": "source-ad", "name": "Ad A"},
            "new-set",
            copy_suffix=EXPECTED_SUFFIX,
        )
        self.assertEqual("Campaign A" + EXPECTED_SUFFIX, campaign["name"])
        self.assertEqual("Ad Set A" + EXPECTED_SUFFIX, adset["name"])
        self.assertEqual("Ad A" + EXPECTED_SUFFIX, ad["name"])
        self.assertEqual(6, client.write_count)
        self.assertTrue(all(item[1].get("status_option") == "PAUSED" for item in client.posts if item[0].endswith("/copies")))

    def test_rename_readback_mismatch_retains_new_id_for_quarantine(self):
        class MismatchClient(NamingClient):
            def get(self, object_id, fields):
                result = super().get(object_id, fields)
                result["name"] = "wrong-name"
                return result

        state = {"campaign": None, "adsets": {}, "ads": {}}
        executor = FacebookLiveExecutor(lambda: None, lambda: None, lambda: None)
        with self.assertRaises(AdControlV3Error) as raised:
            executor._copy_campaign(
                MismatchClient(),
                "source-c",
                state,
                source_name="Campaign A",
                copy_suffix=EXPECTED_SUFFIX,
            )
        self.assertEqual("copy_name_readback_failed", raised.exception.code)
        self.assertEqual("new-c", state["campaign"]["id"])


class TreeWiringExecutor(FacebookLiveExecutor):
    def __init__(self):
        super().__init__(lambda: None, lambda: None, lambda: None, clock=lambda: FIXED_UTC)
        self.created = []

    def _copy_campaign(self, client, source_campaign_id, state=None, *, source_name="", copy_suffix=""):
        value = {"id": "new-c", "name": copied_object_name(source_name, copy_suffix)}
        self.created.append(("campaign", source_campaign_id, source_name, copy_suffix))
        if state is not None:
            state["campaign"] = value
        return value

    def _copy_adset(self, client, source_adset_id, target_campaign_id, state=None, *, source_name="", copy_suffix=""):
        value = {"id": "new-set", "name": copied_object_name(source_name, copy_suffix)}
        self.created.append(("adset", source_adset_id, source_name, copy_suffix))
        if state is not None:
            state.setdefault("adsets", {})[source_adset_id] = value
        return value

    def _copy_ad(self, client, source_ad, target_adset_id, state=None, *, copy_suffix=""):
        source_id = str(source_ad["id"])
        value = {"id": "new-ad", "name": copied_object_name(source_ad["name"], copy_suffix), "creative_id": "creative-1"}
        self.created.append(("ad", source_id, source_ad["name"], copy_suffix))
        if state is not None:
            state.setdefault("ads", {})[source_id] = value
        return value

    def _apply_adjustments(self, client, graph, copied_campaign, copied_adsets, budget, roas):
        return {"campaign": copied_campaign, "adsets": dict(copied_adsets)}


class CopyTreeWiringTests(unittest.TestCase):
    def test_every_new_object_in_each_carrier_uses_one_shared_suffix(self):
        graph = {
            "campaign": {"id": "source-c", "name": "Campaign A"},
            "adsets": [{"id": "source-set", "name": "Ad Set A"}],
            "ads": [{"id": "source-ad", "name": "Ad A", "adset_id": "source-set"}],
        }
        cases = (
            ("campaign", "deep_copy_campaign", ["campaign", "adset", "ad"]),
            ("adset", "new_campaign", ["campaign", "adset", "ad"]),
            ("adset", "same_campaign", ["adset", "ad"]),
            ("ad", "isolated_campaign", ["campaign", "adset", "ad"]),
            ("ad", "isolated_adset", ["adset", "ad"]),
        )
        budget = {
            "budget_level": "adset",
            "budget_type": "daily_budget",
            "campaign_budget": 0,
            "adset_budgets": {"source-set": 100},
        }
        for level, carrier, expected_levels in cases:
            with self.subTest(level=level, carrier=carrier):
                executor = TreeWiringExecutor()
                result = executor._copy_tree(
                    object(),
                    {"object_level": level, "copy_parameters": {"carrier_strategy": carrier}},
                    graph,
                    budget,
                    {},
                )
                self.assertEqual(expected_levels, [item[0] for item in executor.created])
                self.assertEqual({EXPECTED_SUFFIX}, {item[3] for item in executor.created})
                self.assertEqual(EXPECTED_SUFFIX, result["copy_name_suffix"])


class LedgerCursor:
    COLUMNS = [
        "id", "campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id", "ad_name",
        "creative_id", "status", "budget_level", "budget", "latest_budget", "bid_type",
        "bid_control", "bid_amount", "start_time", "local_status", "campaign_action_at",
        "adset_action_at", "ad_action_at", "created_at", "updated_at",
    ]

    def __init__(self, connection):
        self.connection = connection
        self._rows = []
        self.rowcount = 1
        self.lastrowid = 0

    def execute(self, sql, params=()):
        values = tuple(params)
        self.connection.calls.append((sql, values))
        if sql.startswith("SHOW COLUMNS"):
            self._rows = [{"Field": item} for item in self.COLUMNS]
        elif sql.startswith("INSERT INTO `ads_ai`.`ads_facebook_auto_created_data`"):
            self.connection.created_data = dict(zip(self.COLUMNS[1:], values))
            self.lastrowid = 9001
            self._rows = []
        elif sql.startswith("SELECT COUNT(*)"):
            self._rows = [{"total": 1}]
        else:
            self._rows = []
        return self.rowcount

    def fetchall(self):
        return list(self._rows)


class LedgerConnection:
    def __init__(self):
        self.calls = []
        self.created_data = {}
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return LedgerCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        return None


class CopyPersistenceTests(unittest.TestCase):
    def test_created_data_names_are_exact_meta_readback_names(self):
        connection = LedgerConnection()
        executor = FacebookLiveExecutor(lambda: None, lambda: None, lambda: connection)
        graph = {
            "campaign": {"id": "source-c", "name": "Campaign A"},
            "adsets": [{"id": "source-set", "name": "Ad Set A"}],
            "ads": [{"id": "source-ad", "name": "Ad A", "adset_id": "source-set"}],
            "source_rows": {"source-ad": {"id": 42, "start_time": "2026-07-17 10:00:00"}},
        }
        copied = {
            "campaign": {"id": "new-c", "name": "Campaign A" + EXPECTED_SUFFIX},
            "adsets": {
                "source-set": {
                    "id": "new-set",
                    "name": "Ad Set A" + EXPECTED_SUFFIX,
                    "daily_budget": 1234,
                    "bid_strategy": "LOWEST_COST_WITH_MIN_ROAS",
                    "bid_constraints": {"roas_average_floor": 17500},
                    "start_time": "2026-07-17T10:00:00+0800",
                }
            },
            "ads": {
                "source-ad": {
                    "id": "new-ad",
                    "name": "Ad A" + EXPECTED_SUFFIX,
                    "creative_id": "creative-1",
                }
            },
            "copy_name_suffix": EXPECTED_SUFFIX,
            "budget": {
                "budget_level": "adset",
                "budget_type": "daily_budget",
                "campaign_budget": 0,
                "adset_budgets": {"source-set": 1234},
            },
            "roas": {"source-set": 17500},
        }
        result = executor._write_ledger(
            "intent-1",
            {"group_id": "group-1", "owner_user_id": "owner-1", "optimizer_id": 248},
            {"control_rule_id": "rule-1"},
            graph,
            copied,
        )
        self.assertEqual(1, result["row_count"])
        self.assertEqual("Campaign A" + EXPECTED_SUFFIX, connection.created_data["campaign_name"])
        self.assertEqual("Ad Set A" + EXPECTED_SUFFIX, connection.created_data["adset_name"])
        self.assertEqual("Ad A" + EXPECTED_SUFFIX, connection.created_data["ad_name"])
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)

    def test_completed_duplicate_intent_returns_before_name_or_meta_writes(self):
        class Client:
            write_count = 0

        class Executor(FacebookLiveExecutor):
            def __init__(self):
                self.clock_calls = 0
                super().__init__(
                    lambda: None,
                    lambda: None,
                    lambda: None,
                    copy_enabled=True,
                    persistence_enabled=True,
                    clock=self._clock,
                )

            def _clock(self):
                self.clock_calls += 1
                return FIXED_UTC

            def _verify_created_data_schema(self):
                return None

            def _source_rows(self, target):
                return [{"id": 1, "ad_id": "source-ad"}]

            def _token(self, product, source_rows):
                return "token"

            def _client(self, token):
                return Client()

            def _source_graph(self, client, target, source_rows):
                return {"campaign": {}, "adsets": [], "ads": [], "source_rows": {}}

            def _budget_plan(self, target, graph):
                return {}

            def _roas_plan(self, target, graph):
                return {}

            def _reserve_intent(self, group, target, graph):
                return {
                    "reserved": False,
                    "reason": "duplicate_intent",
                    "intent": {"intent_id": "done-1", "status": "completed"},
                }

        executor = Executor()
        result = executor._copy({}, {"object_level": "ad", "object_id": "source-ad", "product": "Dramawave"})
        self.assertEqual("duplicate_completed_intent", result["reason"])
        self.assertEqual(0, result["meta_write_count"])
        self.assertEqual(0, executor.clock_calls)

    def test_name_failure_quarantines_recorded_objects_before_any_ledger_or_activation(self):
        class Client:
            write_count = 2

        class Executor(FacebookLiveExecutor):
            def __init__(self):
                super().__init__(
                    lambda: None,
                    lambda: None,
                    lambda: None,
                    copy_enabled=True,
                    persistence_enabled=True,
                    activation_enabled=True,
                    clock=lambda: FIXED_UTC,
                )
                self.quarantined = None
                self.intent_updates = []

            def _verify_created_data_schema(self):
                return None

            def _source_rows(self, target):
                return [{"id": 1, "ad_id": "source-ad"}]

            def _token(self, product, source_rows):
                return "token"

            def _client(self, token):
                return Client()

            def _source_graph(self, client, target, source_rows):
                return {"campaign": {}, "adsets": [], "ads": [], "source_rows": {}}

            def _budget_plan(self, target, graph):
                return {}

            def _roas_plan(self, target, graph):
                return {}

            def _reserve_intent(self, group, target, graph):
                return {"reserved": True, "intent": {"intent_id": "intent-1"}}

            def _copy_tree(self, client, target, graph, budget, roas, state=None):
                state["campaign"] = {"id": "new-c"}
                raise AdControlV3Error("copy_name_readback_failed", "injected", status=502)

            def _pause_created(self, client, copied):
                self.quarantined = copy.deepcopy(copied)

            def _update_intent(self, intent_id, status, result, error=None):
                self.intent_updates.append((intent_id, status, copy.deepcopy(result), error.code if error else ""))

            def _write_ledger(self, *args, **kwargs):
                raise AssertionError("ledger must not run")

            def _activate(self, *args, **kwargs):
                raise AssertionError("activation must not run")

        executor = Executor()
        result = executor.execute(
            {},
            {"action": "copy", "object_level": "campaign", "object_id": "source-c", "product": "Dramawave"},
        )
        self.assertEqual("failed", result["status"])
        self.assertEqual("copy_name_readback_failed", result["reason"])
        self.assertEqual("new-c", executor.quarantined["campaign"]["id"])
        self.assertEqual("quarantined", executor.intent_updates[-1][1])


class Utc8AuditTests(unittest.TestCase):
    def test_service_meta_declares_storage_and_display_timezones(self):
        service, _, _ = make_service()
        self.assertEqual(
            {
                "storage_timezone": "UTC",
                "display_timezone": "UTC+8",
                "iana_timezone": "Asia/Shanghai",
            },
            service.meta(NORMAL)["time_standard"],
        )

    def test_utc_storage_converts_to_explicit_utc8_and_date_bounds(self):
        self.assertEqual("2026-07-17T14:55:00+08:00", utc8_iso_text(FIXED_UTC))
        self.assertEqual("2026-07-17T10:55:00+08:00", utc8_iso_text("2026-07-17T10:55:00+0800"))
        self.assertEqual("2026-07-16", utc8_business_date("2026-07-16 15:59:59.000000"))
        self.assertEqual("2026-07-17", utc8_business_date("2026-07-16 16:00:00.000000"))
        self.assertEqual(
            ("2026-07-16 16:00:00", "2026-07-17 16:00:00"),
            utc8_date_bounds("2026-07-17", "2026-07-17"),
        )

    def test_api_conversion_is_recursive_and_does_not_shift_date_only_values(self):
        result = convert_audit_times(
            {
                "created_at": "2026-07-17 06:55:00.000000",
                "business_date": "2026-07-17",
                "timeline": [{"at": "2026-07-17T06:56:00Z"}],
                "start_time": "2026-07-17T10:00:00+0800",
            }
        )
        self.assertEqual("2026-07-17T14:55:00+08:00", result["created_at"])
        self.assertEqual("2026-07-17", result["business_date"])
        self.assertEqual("2026-07-17T14:56:00+08:00", result["timeline"][0]["at"])
        self.assertEqual("2026-07-17T10:00:00+0800", result["start_time"])

    def test_json_response_declares_utc8_and_serializes_audit_time(self):
        class Handler:
            def __init__(self):
                self.wfile = io.BytesIO()
                self.headers = {}

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers[name] = value

            def end_headers(self):
                return None

        handler = Handler()
        routes._send_json(handler, 200, {"created_at": "2026-07-17 06:55:00.000000"})
        self.assertEqual("UTC+8", handler.headers["X-Ad-Control-Timezone"])
        self.assertEqual("2026-07-17T14:55:00+08:00", json.loads(handler.wfile.getvalue())["created_at"])

    def test_memory_execution_date_filter_uses_utc8_calendar_day(self):
        repository = MemoryRepository()
        repository.executions = {
            "before": {"execution_id": "before", "created_at": "2026-07-16 15:59:59.000000", "optimizer_id": 1, "targets": []},
            "inside": {"execution_id": "inside", "created_at": "2026-07-16 16:00:00.000000", "optimizer_id": 1, "targets": []},
            "after": {"execution_id": "after", "created_at": "2026-07-17 15:59:59.000000", "optimizer_id": 1, "targets": []},
            "next": {"execution_id": "next", "created_at": "2026-07-17 16:00:00.000000", "optimizer_id": 1, "targets": []},
        }
        result = repository.list_executions({"date_from": "2026-07-17", "date_to": "2026-07-17"})
        self.assertEqual({"inside", "after"}, {item["execution_id"] for item in result["items"]})

    def test_runner_stdout_declares_utc8(self):
        env = dict(os.environ)
        env["AD_CONTROL_V3_RUNNER_ENABLED"] = "0"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ad_control_v3_runner.py")],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("UTC+8", payload["display_timezone"])
        self.assertTrue(payload["ran_at"].endswith("+08:00"))


if __name__ == "__main__":
    unittest.main()
