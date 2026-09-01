#!/usr/bin/env python3
"""Offline contract/state-machine tests; no production DB or Feishu calls."""

import ast
import contextlib
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.material_replication_broadcast import service
from features.material_status_broadcast import service as legacy


def valid_item(**changes):
    item = {
        "resource_id": "resource-001", "resource_name": "暮色心约",
        "original_material_id": "source-001", "original_material_name": "原始素材.mp4",
    }
    item.update(changes)
    return item


def valid_payload(failed=False, **changes):
    item = valid_item()
    if failed:
        item["failed_languages"] = ["法语", "日语"]
    payload = {"event_type": "replication_failed" if failed else "replication_started",
               "editor_username": "剪辑甲", "items": [item]}
    payload.update(changes)
    return payload


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class ValidationTests(unittest.TestCase):
    def invalid(self, payload):
        with self.assertRaises(service.ReplicationError) as caught:
            service.normalize_payload(payload)
        self.assertEqual(caught.exception.code, "invalid_payload")
        self.assertEqual(caught.exception.status, 422)

    def test_normalization_nfc_stripping_and_object_key_order(self):
        first = valid_payload(editor_username="  cafe\u0301  ")
        first["items"][0]["resource_name"] = "  cafe\u0301  "
        second = valid_payload(editor_username="café")
        second["items"][0]["resource_name"] = "café"
        second = dict(reversed(list(second.items())))
        second["items"][0] = dict(reversed(list(second["items"][0].items())))
        normalized = service.normalize_payload(first)
        self.assertEqual(normalized["editor_username"], "café")
        self.assertEqual(normalized["items"][0]["resource_name"], "café")
        self.assertEqual(tuple(normalized), service.PAYLOAD_FIELDS)
        self.assertEqual(service.payload_hash(first), service.payload_hash(second))
        self.assertNotIn("\\u", service.canonical_payload_json(first))

    def test_empty_editor_allowed_but_required(self):
        self.assertEqual(service.normalize_payload(valid_payload(editor_username="  "))["editor_username"], "")
        missing = valid_payload()
        del missing["editor_username"]
        self.invalid(missing)
        for value in (None, 1, True, [], {}):
            with self.subTest(value=value):
                self.invalid(valid_payload(editor_username=value))

    def test_exact_top_level_contract_and_event_types(self):
        for invalid in (None, [], "text", True, {}, valid_payload(extra="x"),
                        valid_payload(event_type="replication_complete"),
                        valid_payload(event_type=""), valid_payload(event_type=1)):
            with self.subTest(payload=invalid):
                self.invalid(invalid)
        for field in service.PAYLOAD_FIELDS:
            payload = valid_payload()
            del payload[field]
            self.invalid(payload)

    def test_exact_item_contract_and_full_batch_atomic_validation(self):
        for value in (None, [], "x", 1, True):
            self.invalid(valid_payload(items=[value]))
        for field in service.ITEM_FIELDS:
            item = valid_item()
            del item[field]
            self.invalid(valid_payload(items=[valid_item(), item]))
        self.invalid(valid_payload(items=[valid_item(extra="x")]))
        self.invalid(valid_payload(items=[valid_item(failed_languages=["法语"])]))
        self.invalid(valid_payload(failed=True, items=[valid_item()]))

    def test_string_types_emptiness_and_field_boundaries(self):
        for field in service.ITEM_FIELDS:
            limit = 128 if field.endswith("_id") else 255
            service.normalize_payload(valid_payload(items=[valid_item(**{field: "x" * limit})]))
            for value in ("x" * (limit + 1), "", "  ", None, 123, True, b"id", []):
                with self.subTest(field=field, value=value):
                    self.invalid(valid_payload(items=[valid_item(**{field: value})]))
        service.normalize_payload(valid_payload(editor_username="x" * 100))
        self.invalid(valid_payload(editor_username="x" * 101))

    def test_single_line_control_and_invalid_unicode_rejected(self):
        for value in ("a\nb", "a\rb", "a\tb", "\na", "a\x00", "a\x7f", "a\x85",
                      "a\u2028b", "a\u2029b", "a\u200eb", "\ud800", "\udfff"):
            with self.subTest(value=repr(value)):
                self.invalid(valid_payload(items=[valid_item(resource_name=value)]))
        service.normalize_payload(valid_payload(items=[valid_item(resource_name="🎬测试🚀")]))

    def test_item_count_boundaries(self):
        for count in (1, 50):
            self.assertEqual(len(service.normalize_payload(valid_payload(items=[valid_item()] * count))["items"]), count)
        for value in ([], [valid_item()] * 51, {}, "x", (valid_item(),)):
            self.invalid(valid_payload(items=value))

    def test_failed_language_boundaries(self):
        for languages in (["x" * 100], ["法语"] * 32):
            payload = valid_payload(failed=True)
            payload["items"][0]["failed_languages"] = languages
            self.assertEqual(service.normalize_payload(payload)["items"][0]["failed_languages"], languages)
        for languages in ([], ["法语"] * 33, ["x" * 101], [""], ["  "],
                          [None], [True], [1], "法语", {"lang": "法语"}):
            payload = valid_payload(failed=True)
            payload["items"][0]["failed_languages"] = languages
            self.invalid(payload)

    def test_item_and_language_order_duplicates_are_preserved(self):
        first = valid_payload(failed=True)
        first["items"][0]["failed_languages"] = ["日语", "法语", "日语"]
        first["items"].append(copy.deepcopy(first["items"][0]))
        normalized = service.normalize_payload(first)
        self.assertEqual(normalized["items"], first["items"])
        second = copy.deepcopy(first)
        second["items"][0]["failed_languages"] = ["法语", "日语", "日语"]
        self.assertNotEqual(service.payload_hash(first), service.payload_hash(second))
        third = valid_payload(items=[valid_item(), valid_item(resource_id="resource-002")])
        fourth = copy.deepcopy(third)
        fourth["items"].reverse()
        self.assertNotEqual(service.payload_hash(third), service.payload_hash(fourth))
        self.assertNotEqual(service.payload_hash(first), service.payload_hash(valid_payload(failed=True)))

    def test_shared_error_and_auth_helpers(self):
        self.assertIs(service.ReplicationError, legacy.MaterialStatusError)
        self.assertTrue(service.validate_bearer_authorization("Bearer independent-secret", "independent-secret"))
        self.assertFalse(service.validate_bearer_authorization("Bearer legacy-secret", "independent-secret"))
        self.assertEqual(service.validate_idempotency_key("batch:20260827:001"), "batch:20260827:001")
        for key in (None, "short", "a b c d e", "非法幂等键00000"):
            with self.assertRaises(service.ReplicationError):
                service.validate_idempotency_key(key)


class RenderingTests(unittest.TestCase):
    def test_started_template_exact(self):
        expected = "\n".join([
            "【自动复刻任务已发起】", "", "以下素材已自动发起复刻任务：", "",
            "1. 资源ID：resource-001", "   资源名：暮色心约",
            "   原始素材ID：source-001", "   原始素材名：原始素材.mp4", "",
            "素材语种包含：西班牙语、法语、阿拉伯语、俄语、葡萄牙语、日语、繁体中文、泰语、印度尼西亚语、德语、越南语、意大利语、土耳其语、波兰语、罗马尼亚语、捷克语、韩语。",
            "", "批次编号：MRB-0000000012",
        ])
        self.assertEqual(service.format_private_message(valid_payload(), 12), expected)
        self.assertEqual(len(service.REPLICATION_LANGUAGES), 17)

    def test_failed_template_and_numbered_duplicates(self):
        payload = valid_payload(failed=True)
        payload["items"][0]["failed_languages"] = ["日语", "法语", "日语"]
        payload["items"].append(copy.deepcopy(payload["items"][0]))
        rendered = service.format_private_message(payload, 13)
        self.assertTrue(rendered.startswith("【自动复刻失败】\n\n以下素材自动复刻失败："))
        self.assertIn("1. 资源ID：resource-001", rendered)
        self.assertIn("2. 资源ID：resource-001", rendered)
        self.assertEqual(rendered.count("失败语种：日语、法语、日语"), 2)
        self.assertIn("备注：复刻失败一般是算法失败，重试基本也不会成功。", rendered)
        self.assertTrue(rendered.endswith("批次编号：MRB-0000000013"))
        self.assertNotIn("素材语种包含", rendered)

    def test_fallback_uses_frozen_private_text_and_redacts_diagnostics(self):
        frozen = "【旧版已冻结文本】\n批次编号：MRB-0000000021"
        reason = "person@example.com Bearer top-secret open_id=ou_123456789012 token=secret"
        with mock.patch.object(service, "format_private_message", side_effect=AssertionError("rerender forbidden")):
            message = service.format_fallback_message(
                frozen, "synthetic_reason", reason, " editor_qa ",
            )
        self.assertTrue(message.startswith("【⚠️ 自动复刻播报未能私聊】"))
        self.assertTrue(message.endswith(frozen))
        self.assertIn("收到的 username：editor_qa", message)
        self.assertIn("失败原因：synthetic_reason", message)
        for secret in ("person@example.com", "top-secret", "ou_123456789012", "token=secret"):
            self.assertNotIn(secret, message)
        known = service.format_fallback_message(frozen, "editor_username_missing", "unsafe detail")
        self.assertIn("收到的 username：（空）", known)
        self.assertIn("说明：接口未提供剪辑用户名", known)
        self.assertNotIn("unsafe detail", known)

    def test_adapter_failures_have_chinese_operator_instructions(self):
        expected = {
            "message_send_failed": "私聊投递已明确失败，请检查机器人可用范围及消息发送权限",
            "user_lookup_failed": "飞书用户查询失败，请检查应用通讯录查询权限及可用范围",
            "user_lookup_invalid_response": "飞书用户查询返回异常数据，请联系管理员检查用户查询接口",
            "feishu_lookup_unavailable": "飞书用户查询服务暂不可用，请检查网络连接和飞书应用凭据",
            "feishu_user_lookup_unavailable": "飞书用户查询服务暂不可用，请检查网络连接和飞书应用凭据",
            "feishu_receive_id_missing": "未取得有效的飞书收件人标识，请检查剪辑用户邮箱与飞书用户映射",
            "feishu_message_uuid_invalid": "消息幂等标识无效，请联系管理员检查播报消息生成逻辑",
            "feishu_send_unavailable": "飞书发送结果暂无法确认，请联系管理员核查消息投递记录",
            "message_send_invalid_response": "飞书未返回有效的投递确认，请联系管理员核查消息投递记录",
            "message_send_unknown": "飞书发送结果暂无法确认，请联系管理员核查消息投递记录",
            "delivery_failed": "飞书投递失败，请联系管理员检查投递配置和应用权限",
        }
        frozen = service.format_private_message(valid_payload(), 17)
        remote_error = "Feishu request failed: http=400 code=99991672 message=permission denied"
        for code, explanation in expected.items():
            with self.subTest(code=code):
                rendered = service.format_fallback_message(frozen, code, remote_error)
                self.assertIn("失败原因：%s" % code, rendered)
                self.assertIn("说明：%s\n" % explanation, rendered)
                self.assertTrue(rendered.endswith(frozen))
                self.assertNotIn(remote_error, rendered)
                self.assertNotIn("permission denied", rendered)
                self.assertLessEqual(len(explanation), 200)

    def test_current_adapter_error_codes_all_have_chinese_labels(self):
        # Inspect definitions without importing app.py or starting any runtime.
        functions = {
            "run_material_status_mapping_query",
            "resolve_material_status_optimizer_from_database",
            "lookup_material_status_feishu_open_id",
            "send_material_status_feishu_text",
        }
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
        found = set()
        codes = set()
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef) or function.name not in functions:
                continue
            found.add(function.name)
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "MaterialStatusDeliveryError" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    codes.add(node.args[0].value)
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if (isinstance(key, ast.Constant) and key.value == "code"
                                and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                            codes.add(value.value)
        self.assertEqual(found, functions)
        # material_status_feishu_response formats these operation-specific codes.
        codes.update(operation + suffix for operation in ("user_lookup", "message_send")
                     for suffix in ("_failed", "_invalid_response"))
        self.assertIn("feishu_lookup_unavailable", codes)
        self.assertFalse(codes.difference(service._FALLBACK_REASON_LABELS),
                         "Adapter error codes missing Chinese fallback descriptions: %s" %
                         sorted(codes.difference(service._FALLBACK_REASON_LABELS)))

    def test_batch_id_boundaries(self):
        self.assertEqual(service.format_batch_id(1), "MRB-0000000001")
        self.assertEqual(service.format_batch_id("12"), "MRB-0000000012")
        self.assertEqual(service.format_batch_id(9223372036854775807), "MRB-9223372036854775807")
        for invalid in (None, True, False, 0, -1, 1.5, "1.5", "", "-1", 9223372036854775808):
            with self.assertRaises(service.ReplicationError):
                service.format_batch_id(invalid)

    def test_wire_size_matches_outer_ascii_encoding_and_full_reserved_envelope(self):
        private = "测试🚀\"\\" * 100
        fallback = service.format_fallback_message(
            private, "r" * 64, "🚀" * 200, "🚀" * 100,
        )
        request = {
            "receive_id": "🚀" * 128, "msg_type": "text",
            "content": json.dumps({"text": fallback}, ensure_ascii=False), "uuid": "u" * 50,
        }
        expected = len(json.dumps(request, ensure_ascii=True, allow_nan=False).encode("utf-8"))
        self.assertEqual(service.validate_message_size(private), expected)
        self.assertGreater(expected, len(json.dumps(request, ensure_ascii=False).encode("utf-8")))
        self.assertEqual(service.MAX_REQUEST_BYTES, 32768)
        self.assertEqual(service.MAX_FEISHU_REQUEST_BYTES, 131072)

    def test_wire_size_boundary_includes_surrogate_pair_ascii_expansion(self):
        low, high = 1, 20000
        while low < high:
            middle = (low + high + 1) // 2
            try:
                service.validate_message_size("🚀" * middle)
            except service.ReplicationError as exc:
                self.assertEqual(exc.code, "message_too_large")
                high = middle - 1
            else:
                low = middle
        self.assertLessEqual(service.validate_message_size("🚀" * low), 131072)
        with self.assertRaises(service.ReplicationError) as caught:
            service.validate_message_size("🚀" * (low + 1))
        self.assertEqual(caught.exception.code, "message_too_large")
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaises(service.ReplicationError):
            service.validate_message_size("\ud800")


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="replication-outbox-test-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "jobs.sqlite3"
        self.clock = MutableClock()
        self.store = service.ReplicationOutbox(self.path, clock=self.clock)

    def enqueue(self, key="replication-test-001", **kwargs):
        return self.store.enqueue(key, valid_payload(), **kwargs)

    def claimed(self, **kwargs):
        row = self.enqueue(**kwargs)
        claimed = self.store.claim_next()
        self.assertEqual(claimed["id"], row["id"])
        return claimed

    def sending(self, **kwargs):
        row = self.claimed(**kwargs)
        self.store.freeze_target(row["id"], row["lease_id"], "recipient-private-test")
        return self.store.begin_send(row["id"], row["lease_id"])

    def count(self):
        with contextlib.closing(sqlite3.connect(str(self.path))) as connection:
            return connection.execute("SELECT COUNT(*) FROM material_replication_broadcast_outbox").fetchone()[0]

    def test_enqueue_freezes_text_and_uuid_but_not_recipient(self):
        row = self.enqueue(source_ip="2001:0db8::1")
        self.assertTrue(row["created"])
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["phase"], "private")
        self.assertEqual(row["private_text"], row["message_text"])
        self.assertEqual(row["private_text"], service.format_private_message(valid_payload(), row["id"]))
        self.assertEqual(row["message_uuid"], "mrb-1-private")
        self.assertEqual(row["receive_id"], "")
        self.assertEqual(row["source_ip"], "2001:db8::1")
        self.assertEqual(row["delivery_kind"], "")
        self.assertFalse(row["uncertain"])
        self.assertEqual(row["attempt_count"], 0)
        self.assertEqual(row["max_attempts"], 5)
        row["payload"]["items"][0]["resource_name"] = "mutated locally"
        self.assertEqual(self.store.get(row["id"])["payload"], valid_payload())
        self.assertIsNone(self.store.get(1000))

    def test_idempotency_normalizes_body_and_rejects_conflict(self):
        first = self.enqueue()
        normalized = valid_payload(editor_username="  剪辑甲  ")
        second = self.store.enqueue("replication-test-001", normalized, source_ip="198.51.100.7", max_attempts=2)
        self.assertFalse(second["created"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["max_attempts"], 5)
        self.assertEqual(second["source_ip"], "")
        with self.assertRaises(service.ReplicationError) as caught:
            self.store.enqueue("replication-test-001", valid_payload(failed=True))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.count(), 1)

    def test_terminal_idempotency_never_creates_another_send(self):
        for index, terminal in enumerate(("delivered", "dead_letter", "delivery_unknown")):
            key = "replication-terminal-%d" % index
            row = self.claimed(key=key)
            if terminal == "delivered":
                self.store.freeze_target(row["id"], row["lease_id"], "recipient-private-test")
                self.store.begin_send(row["id"], row["lease_id"])
                self.store.delivered(row["id"], row["lease_id"], "om_test_confirmed")
            elif terminal == "dead_letter":
                self.store.dead_letter(row["id"], row["lease_id"], "invalid_mapping", "已终止")
            else:
                self.store.unknown(row["id"], row["lease_id"], "unknown_send", "需核查")
            with mock.patch.object(service, "format_private_message", side_effect=AssertionError("must retain frozen text")):
                replay = self.store.enqueue(key, valid_payload())
            self.assertFalse(replay["created"])
            self.assertEqual(replay["status"], terminal)
            self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.count(), 3)

    def test_atomic_invalid_and_oversize_rejections_leave_no_rows(self):
        invalid = valid_payload(items=[valid_item(), valid_item(resource_id=1)])
        with self.assertRaises(service.ReplicationError):
            self.store.enqueue("replication-invalid-001", invalid)
        huge = valid_payload(failed=True)
        huge["items"][0]["failed_languages"] = ["🚀" * 100] * 32
        huge["items"] *= 50
        with self.assertRaises(service.ReplicationError) as caught:
            self.store.enqueue("replication-huge-001", huge)
        self.assertEqual(caught.exception.code, "message_too_large")
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(self.count(), 0)

    def test_legacy_queue_and_same_idempotency_key_are_isolated(self):
        old_store = legacy.MaterialStatusOutbox(self.path, clock=self.clock)
        old_payload = {
            "resource_id": "legacy-res", "resource_name": "旧资源",
            "task_start_time": "2026-08-27T04:00:00Z", "drama_dubbing_type": "AI配音",
            "task_type": "素材制作", "original_material_name": "旧原始素材", "material_name": "旧结果",
            "language": "法语", "final_status": "成功", "optimizer_name": "旧用户",
        }
        old = old_store.enqueue("replication-test-001", old_payload)
        with contextlib.closing(sqlite3.connect(str(self.path))) as connection:
            connection.execute("CREATE TABLE unrelated_user_table(k TEXT PRIMARY KEY,v TEXT)")
            connection.execute("INSERT INTO unrelated_user_table VALUES('keep','untouched')")
            connection.commit()
            before = connection.execute("SELECT * FROM material_status_broadcast_outbox").fetchall()
        service.ensure_storage(self.path)
        fresh = self.enqueue()
        self.assertEqual(old["id"], fresh["id"])
        self.assertNotEqual(old["payload"], fresh["payload"])
        with contextlib.closing(sqlite3.connect(str(self.path))) as connection:
            self.assertEqual(connection.execute("SELECT * FROM material_status_broadcast_outbox").fetchall(), before)
            self.assertEqual(connection.execute("SELECT * FROM unrelated_user_table").fetchall(), [("keep", "untouched")])

    def test_concurrent_enqueue_has_single_winner_and_one_row(self):
        def worker(_):
            store = service.ReplicationOutbox(self.path, clock=self.clock)
            return store.enqueue("replication-concurrent-001", valid_payload())
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(worker, range(16)))
        self.assertEqual(sum(row["created"] for row in rows), 1)
        self.assertEqual({row["id"] for row in rows}, {1})
        self.assertEqual(self.count(), 1)

    def test_concurrent_claim_only_one_worker_owns_row(self):
        self.enqueue()
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.store.claim_next(), range(8)))
        claimed = [row for row in rows if row is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["attempt_count"], 1)

    def test_freeze_target_is_idempotent_but_cannot_rebind(self):
        row = self.claimed()
        frozen = self.store.freeze_target(row["id"], row["lease_id"], "recipient-private-test")
        same = self.store.freeze_target(row["id"], row["lease_id"], "recipient-private-test")
        self.assertEqual(frozen["receive_id"], same["receive_id"])
        self.assertEqual(frozen["private_text"], row["private_text"])
        self.assertEqual(frozen["message_uuid"], row["message_uuid"])
        with self.assertRaises(service.ReplicationError) as caught:
            self.store.freeze_target(row["id"], row["lease_id"], "recipient-changed-test")
        self.assertEqual(caught.exception.code, "target_frozen")
        self.assertNotIn("recipient", str(caught.exception))

    def test_recipient_validation(self):
        row = self.claimed()
        for recipient in (None, 123, True, "", "x" * 129, " x", "x ", "a b", "a\nb", "a\u2028b", "\ud800"):
            with self.subTest(recipient=repr(recipient)):
                with self.assertRaises(service.ReplicationError):
                    self.store.freeze_target(row["id"], row["lease_id"], recipient)
        self.store.freeze_target(row["id"], row["lease_id"], "🚀" * 128)

    def test_all_mutations_reject_expired_lease_before_reclaim(self):
        row = self.claimed()
        self.store.freeze_target(row["id"], row["lease_id"], "recipient-private-test")
        self.clock.advance(300)
        before = self.store.get(row["id"])
        actions = (
            lambda: self.store.freeze_target(row["id"], row["lease_id"], "recipient-private-test"),
            lambda: self.store.begin_send(row["id"], row["lease_id"]),
            lambda: self.store.clear_known_failure(row["id"], row["lease_id"], False),
            lambda: self.store.prepare_fallback(row["id"], row["lease_id"], "group-test", "mapping_unavailable"),
            lambda: self.store.retry(row["id"], row["lease_id"], "retry", "retry", 0),
            lambda: self.store.delivered(row["id"], row["lease_id"], "om_test"),
            lambda: self.store.dead_letter(row["id"], row["lease_id"], "dead", "dead"),
            lambda: self.store.unknown(row["id"], row["lease_id"], "unknown", "unknown"),
        )
        for action in actions:
            with self.assertRaises(service.ReplicationError) as caught:
                action()
            self.assertEqual(caught.exception.code, "outbox_lease_conflict")
        self.assertEqual(before, self.store.get(row["id"]))
        reclaimed = self.store.claim_next()
        self.assertEqual(reclaimed["id"], row["id"])
        self.assertNotEqual(reclaimed["lease_id"], row["lease_id"])
        self.assertEqual(reclaimed["attempt_count"], 2)
        with self.assertRaises(service.ReplicationError):
            self.store.retry(row["id"], row["lease_id"], "stale", "stale", 0)

    def test_begin_send_persists_marker_and_renews_lease(self):
        row = self.claimed()
        with self.assertRaises(service.ReplicationError):
            self.store.begin_send(row["id"], row["lease_id"])
        self.store.freeze_target(row["id"], row["lease_id"], "recipient-private-test")
        self.clock.advance(250)
        sending = self.store.begin_send(row["id"], row["lease_id"])
        self.assertTrue(sending["uncertain"])
        self.assertFalse(sending["previous_uncertain"])
        self.assertEqual(sending["first_uncertain_at"], "2026-08-27T04:04:10.000000Z")
        self.assertEqual(sending["lease_expires_at"], "2026-08-27T04:09:10.000000Z")
        reopened = service.ReplicationOutbox(self.path, self.clock).get(row["id"])
        self.assertTrue(reopened["uncertain"])
        self.assertEqual(reopened["first_uncertain_at"], sending["first_uncertain_at"])

    def test_known_not_sent_clears_only_new_uncertainty_and_resets_clock(self):
        row = self.sending()
        cleared = self.store.clear_known_failure(row["id"], row["lease_id"], row["previous_uncertain"])
        self.assertFalse(cleared["uncertain"])
        self.assertEqual(cleared["first_uncertain_at"], "")
        self.store.retry(row["id"], row["lease_id"], "rate_limit", "稍后重试", 5)
        self.clock.advance(5)
        next_row = self.store.claim_next()
        begun = self.store.begin_send(next_row["id"], next_row["lease_id"])
        self.assertFalse(begun["previous_uncertain"])
        self.assertEqual(begun["first_uncertain_at"], "2026-08-27T04:00:05.000000Z")

    def test_prior_unknown_is_sticky_even_if_caller_flag_is_wrong(self):
        row = self.sending()
        self.store.retry(row["id"], row["lease_id"], "network_unknown", "响应丢失", 0)
        second = self.store.claim_next()
        # Even before begin_send, a reclaimed prior marker cannot be erased.
        still = self.store.clear_known_failure(second["id"], second["lease_id"], False)
        self.assertTrue(still["uncertain"])
        second = self.store.begin_send(second["id"], second["lease_id"])
        self.assertTrue(second["previous_uncertain"])
        still = self.store.clear_known_failure(second["id"], second["lease_id"], False)
        self.assertTrue(still["uncertain"])
        self.assertEqual(still["first_uncertain_at"], row["first_uncertain_at"])
        with self.assertRaises(service.ReplicationError) as caught:
            self.store.prepare_fallback(second["id"], second["lease_id"], "group-test", "private_send_failed")
        self.assertEqual(caught.exception.code, "outbox_uncertain")

    def test_fallback_is_frozen_private_body_with_separate_attempt_budget(self):
        row = self.claimed(max_attempts=2)
        self.store.retry(row["id"], row["lease_id"], "mapping_unavailable", "稍后重试", 0)
        row = self.store.claim_next()
        self.assertEqual(row["attempt_count"], 2)
        with mock.patch.object(service, "format_private_message", side_effect=AssertionError("must not rerender")):
            fallback = self.store.prepare_fallback(row["id"], row["lease_id"], "group-test", "editor_username_missing")
        self.assertEqual(fallback["phase"], "fallback")
        self.assertEqual(fallback["status"], "queued")
        self.assertEqual(fallback["attempt_count"], 0)
        self.assertEqual(fallback["lease_id"], "")
        self.assertEqual(fallback["message_uuid"], "mrb-1-fallback")
        self.assertIn("收到的 username：剪辑甲", fallback["message_text"])
        self.assertNotEqual(fallback["message_uuid"], row["message_uuid"])
        self.assertTrue(fallback["message_text"].endswith(row["private_text"]))
        self.assertEqual(fallback["delivery_kind"], "")
        self.assertLessEqual(len(fallback["message_uuid"]), 50)
        claimed = self.store.claim_next()
        self.assertEqual(claimed["attempt_count"], 1)
        self.assertEqual(claimed["message_text"], fallback["message_text"])
        with self.assertRaises(service.ReplicationError):
            self.store.prepare_fallback(claimed["id"], claimed["lease_id"], "other-group", "retry")
        with self.assertRaises(service.ReplicationError):
            self.store.freeze_target(claimed["id"], claimed["lease_id"], "other-recipient")

    def test_fallback_blocked_after_any_outstanding_send(self):
        row = self.sending()
        with self.assertRaises(service.ReplicationError) as caught:
            self.store.prepare_fallback(row["id"], row["lease_id"], "group-test", "private_send_failed")
        self.assertEqual(caught.exception.code, "outbox_uncertain")
        self.assertEqual(self.store.get(row["id"])["phase"], "private")

    def test_retry_and_reclaim_keep_frozen_target_text_uuid(self):
        row = self.sending()
        self.clock.advance(300)
        reclaimed = self.store.claim_next()
        self.assertTrue(reclaimed["uncertain"])
        for field in ("receive_id", "message_text", "message_uuid", "first_uncertain_at"):
            self.assertEqual(reclaimed[field], row[field])
        second = self.store.begin_send(reclaimed["id"], reclaimed["lease_id"])
        self.assertTrue(second["previous_uncertain"])
        self.assertEqual(second["message_uuid"], row["message_uuid"])

    def test_unknown_window_exact_boundary_is_terminal_and_never_reclaimed(self):
        row = self.sending()
        self.store.retry(row["id"], row["lease_id"], "unknown_network", "响应丢失", 4000)
        self.clock.advance(3300)
        self.assertIsNone(self.store.claim_next())
        ended = self.store.get(row["id"])
        self.assertEqual(ended["status"], "delivery_unknown")
        self.assertEqual(ended["last_error_code"], "delivery_window_expired")
        self.assertEqual(ended["phase"], "private")
        self.assertEqual(ended["delivery_kind"], "")
        self.assertIsNone(self.store.claim_next())

    def test_begin_send_rechecks_window_even_during_live_lease(self):
        row = self.sending()
        self.store.retry(row["id"], row["lease_id"], "unknown_network", "响应丢失", 0)
        self.clock.advance(3299)
        second = self.store.claim_next()
        self.clock.advance(1)
        with self.assertRaises(service.ReplicationError) as caught:
            self.store.begin_send(second["id"], second["lease_id"])
        self.assertEqual(caught.exception.code, "delivery_window_expired")
        self.assertEqual(self.store.get(row["id"])["status"], "delivery_unknown")

    def test_expired_crash_reclaim_exhaustion_distinguishes_unknown(self):
        first = self.sending(max_attempts=1)
        self.clock.advance(300)
        self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.store.get(first["id"])["status"], "delivery_unknown")
        second = self.claimed(key="replication-nosend-002", max_attempts=1)
        self.clock.advance(300)
        self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.store.get(second["id"])["status"], "dead_letter")

    def test_retry_cap_unknown_vs_known_and_direct_dead_letter_guard(self):
        first = self.sending(max_attempts=1)
        ended = self.store.retry(first["id"], first["lease_id"], "network_unknown", "响应丢失", 0)
        self.assertEqual(ended["status"], "delivery_unknown")
        second = self.sending(key="replication-known-002", max_attempts=1)
        self.store.clear_known_failure(second["id"], second["lease_id"], False)
        ended = self.store.retry(second["id"], second["lease_id"], "known_failure", "已确认未发送", 0)
        self.assertEqual(ended["status"], "dead_letter")
        third = self.sending(key="replication-sticky-003")
        ended = self.store.dead_letter(third["id"], third["lease_id"], "mapping_failure", "终止")
        self.assertEqual(ended["status"], "delivery_unknown")

    def test_retry_due_time_and_attempt_cap_per_phase(self):
        row = self.claimed(max_attempts=2)
        self.store.retry(row["id"], row["lease_id"], "retryable", "稍后重试", 60)
        self.clock.advance(59)
        self.assertIsNone(self.store.claim_next())
        self.clock.advance(1)
        row = self.store.claim_next()
        self.assertEqual(row["attempt_count"], 2)
        fallback = self.store.prepare_fallback(row["id"], row["lease_id"], "group-test", "private_send_failed")
        for attempt in (1, 2):
            row = self.store.claim_next()
            self.assertEqual(row["attempt_count"], attempt)
            self.assertEqual(row["message_uuid"], fallback["message_uuid"])
            ended = self.store.retry(row["id"], row["lease_id"], "known_failure", "未发送", 0)
        self.assertEqual(ended["status"], "dead_letter")
        self.assertIsNone(self.store.claim_next())

    def test_confirmed_delivery_kind_only_after_acknowledgement(self):
        first = self.sending()
        self.assertEqual(first["delivery_kind"], "")
        delivered = self.store.delivered(first["id"], first["lease_id"], "om_private_confirmed")
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(delivered["delivery_kind"], "private")
        self.assertFalse(delivered["uncertain"])
        second = self.claimed(key="replication-fallback-002")
        self.store.prepare_fallback(second["id"], second["lease_id"], "group-test", "editor_not_found")
        second = self.store.claim_next()
        self.store.begin_send(second["id"], second["lease_id"])
        delivered = self.store.delivered(second["id"], second["lease_id"], "om_fallback_confirmed")
        self.assertEqual(delivered["delivery_kind"], "fallback")
        self.assertEqual(delivered["feishu_message_id"], "om_fallback_confirmed")
        with self.assertRaises(service.ReplicationError):
            self.store.delivered(second["id"], second["lease_id"], "om_double")

    def test_audit_errors_and_source_ip_are_sanitized(self):
        row = self.claimed(source_ip="person@example.com Bearer secret")
        self.assertEqual(row["source_ip"], "")
        self.store.freeze_target(row["id"], row["lease_id"], "recipient-private-test")
        reason = "recipient-private-test person@example.com Bearer top-secret open_id=ou_123456789012 token=secret"
        failed = self.store.retry(row["id"], row["lease_id"], "recipient-private-test", reason, 0)
        for secret in ("recipient-private-test", "person@example.com", "top-secret", "ou_123456789012", "token=secret"):
            self.assertNotIn(secret, failed["last_error_message"])
            self.assertNotIn(secret, failed["last_error_code"])
        claimed = self.store.claim_next()
        fallback = self.store.prepare_fallback(claimed["id"], claimed["lease_id"], "group-test", "synthetic_code", reason)
        self.assertNotIn("recipient-private-test", fallback["message_text"])

    def test_invalid_runtime_parameters_do_not_mutate_rows(self):
        for maximum in (0, 21, True, 1.2, None, "1.0"):
            with self.assertRaises(service.ReplicationError):
                self.enqueue(max_attempts=maximum)
        self.assertEqual(self.count(), 0)
        row = self.claimed()
        before = self.store.get(row["id"])
        for delay in (-1, 86401, True, 1.2, None):
            with self.assertRaises(service.ReplicationError):
                self.store.retry(row["id"], row["lease_id"], "retry", "retry", delay)
        for seconds in (0, 4, 3601, True, 1.2, None):
            with self.assertRaises(service.ReplicationError):
                self.store.claim_next(seconds)
        self.assertEqual(before, self.store.get(row["id"]))

    def test_untrusted_lease_id_and_recipient_as_message_id_are_rejected(self):
        row = self.sending()
        for lease_id in ("non-ascii-租约", "\ud800", None, True, "0" * 32):
            with self.assertRaises(service.ReplicationError) as caught:
                self.store.retry(row["id"], lease_id, "test", "test", 0)
            self.assertEqual(caught.exception.code, "outbox_lease_conflict")
        for message_id in ("recipient-private-test", "ou_synthetic_test_identifier"):
            with self.assertRaises(service.ReplicationError) as caught:
                self.store.delivered(row["id"], row["lease_id"], message_id)
            self.assertEqual(caught.exception.code, "invalid_message_id")

    def test_recipient_in_fallback_reason_code_is_redacted(self):
        row = self.claimed()
        self.store.freeze_target(row["id"], row["lease_id"], "recipient-private-test")
        fallback = self.store.prepare_fallback(row["id"], row["lease_id"], "group-test", "recipient-private-test")
        self.assertEqual(fallback["fallback_reason_code"], "delivery_error")
        self.assertNotIn("recipient-private-test", fallback["message_text"])

    def test_fallback_keeps_sanitized_audit_diagnostic_but_renders_chinese(self):
        row = self.claimed()
        diagnostic = "Feishu user_lookup failed: http=400 permission denied token=synthetic-secret"
        fallback = self.store.prepare_fallback(
            row["id"], row["lease_id"], "group-test", "user_lookup_failed", diagnostic,
        )
        self.assertIn("permission denied", fallback["last_error_message"])
        self.assertNotIn("synthetic-secret", fallback["last_error_message"])
        self.assertNotIn("permission denied", fallback["message_text"])
        self.assertIn("请检查应用通讯录查询权限及可用范围", fallback["message_text"])
        self.assertTrue(fallback["message_text"].endswith(row["private_text"]))

    def test_existing_idempotency_conflict_precedes_message_size(self):
        self.enqueue()
        huge = valid_payload(failed=True)
        huge["items"][0]["failed_languages"] = ["🚀" * 100] * 32
        huge["items"] *= 50
        with self.assertRaises(service.ReplicationError) as caught:
            self.store.enqueue("replication-test-001", huge)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
