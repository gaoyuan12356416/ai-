#!/usr/bin/env python3
"""Apply the ad-control batched execution fix to a composite live checkout."""

import argparse
import os
from pathlib import Path


def replace_once(text, old, new, label):
    count = text.count(old)
    if count == 0 and new in text:
        print("%s: already applied" % label)
        return text, False
    if count != 1:
        raise RuntimeError("%s: expected one old block, found %s" % (label, count))
    return text.replace(old, new, 1), True


def patch_file(path, replacements, check_only=False):
    original = path.read_text(encoding="utf-8")
    updated = original
    changed = False
    for label, old, new in replacements:
        updated, block_changed = replace_once(updated, old, new, label)
        changed = changed or block_changed
    if changed and not check_only:
        temp_path = path.with_name(path.name + ".batched-execute.tmp")
        temp_path.write_text(updated, encoding="utf-8")
        os.chmod(str(temp_path), path.stat().st_mode)
        os.replace(str(temp_path), str(path))
    print("%s: %s" % (path, "would change" if check_only and changed else "changed" if changed else "unchanged"))
    return changed


APP_REPLACEMENTS = [
    (
        "app execution target selection",
        '''    total = len(items)\n    pause_count = len([item for item in items if item.get("target_action") == "pause"])\n    observe_count = len([item for item in items if item.get("target_action") == "observe"])\n''',
        '''    total = len(items)\n    pause_items = [item for item in items if item.get("target_action") == "pause"]\n    pause_items.sort(key=lambda item: (\n        ad_control_normalize_account(item.get("account_id")),\n        str(item.get("campaign_id") or item.get("object_id") or ""),\n    ))\n    pause_count = len(pause_items)\n    execution_items = pause_items[:AD_CONTROL_MAX_LIVE_EXECUTE]\n    observe_count = len([item for item in items if item.get("target_action") == "observe"])\n''',
    ),
    (
        "app execution metadata",
        '''        "binding_id": scope.get("rule_group_id"),\n        "preview_hash": preview_hash,\n    }\n''',
        '''        "binding_id": scope.get("rule_group_id"),\n        "preview_hash": preview_hash,\n        "execution_target_count": pause_count,\n        "execution_batch_count": len(execution_items),\n        "execution_truncated": pause_count > len(execution_items),\n    }\n''',
    ),
    (
        "app stored execution batch",
        '''                    json.dumps(items[:AD_CONTROL_MAX_LIVE_EXECUTE], ensure_ascii=False),\n''',
        '''                    json.dumps(execution_items, ensure_ascii=False),\n''',
    ),
    (
        "app preview execution counts",
        '''        "pause_count": pause_count,\n        "observe_count": observe_count,\n''',
        '''        "pause_count": pause_count,\n        "execution_count": len(execution_items),\n        "execution_remaining_count": max(0, pause_count - len(execution_items)),\n        "observe_count": observe_count,\n''',
    ),
    (
        "app preview target items",
        '''        "items": items[:200],\n''',
        '''        "items": execution_items[:200],\n''',
    ),
]


RUNNER_REPLACEMENTS = [
    (
        "runner partial result handling",
        '''    status = "error" if int(result.get("error_count") or 0) > 0 else "executed"\n    return event_payload(rule_group, action, event_key, status, result=result, reason="live_execute_errors" if status == "error" else "")\n\n\ndef run_rule_groups():\n''',
        '''    pause_count = int(preview.get("pause_count") or 0)\n    requested_count = int(result.get("requested_count") or 0)\n    remaining_count = max(0, pause_count - requested_count)\n    result["preview_pause_count"] = pause_count\n    result["remaining_target_count"] = remaining_count\n    if int(result.get("error_count") or 0) > 0:\n        status = "error"\n        reason = "live_execute_errors"\n    elif remaining_count > 0:\n        status = "partial"\n        reason = "live_execute_partial"\n    else:\n        status = "executed"\n        reason = ""\n    return event_payload(rule_group, action, event_key, status, result=result, reason=reason)\n\n\ndef group_event_is_continuation(last, action, action_key):\n    event = last.get("last_event") or {}\n    return (\n        event.get("status") == "partial"\n        and event.get("action") == action\n        and event.get("event_key") == action_key\n    )\n\n\ndef run_rule_groups():\n''',
    ),
    (
        "runner continuation gate",
        '''            due, event_key = event_due(now, hhmm)\n            if not due:\n                continue\n            action_key = "%s:%s:%s" % (action, tz_label, event_key)\n            if last_keys.get(action) == action_key:\n                continue\n            try:\n                payload = run_group_event(group, action, action_key)\n''',
        '''            due, event_key = event_due(now, hhmm)\n            action_key = "%s:%s:%s" % (action, tz_label, event_key)\n            continuing = group_event_is_continuation(last, action, action_key)\n            if not due and not continuing:\n                continue\n            if last_keys.get(action) == action_key and not continuing:\n                continue\n            try:\n                payload = run_group_event(group, action, action_key)\n''',
    ),
    (
        "runner completion marker",
        '''            if payload.get("status") != "error" and not execution_has_errors(payload):\n                last_keys[action] = action_key\n            updated = dict(last)\n            updated["last_keys"] = last_keys\n            updated["last_event"] = payload\n            update_rule_group_result(group.get("group_id"), updated)\n''',
        '''            if payload.get("status") == "executed" and not execution_has_errors(payload):\n                last_keys[action] = action_key\n            elif payload.get("status") == "partial":\n                last_keys.pop(action, None)\n            updated = dict(last)\n            updated["last_keys"] = last_keys\n            updated["last_event"] = payload\n            update_rule_group_result(group.get("group_id"), updated)\n''',
    ),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/root/drama_material_service")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(args.root)
    changed = []
    changed.append(patch_file(root / "app.py", APP_REPLACEMENTS, check_only=args.check))
    changed.append(patch_file(root / "scripts" / "ad_control_rule_runner.py", RUNNER_REPLACEMENTS, check_only=args.check))
    print("changed_files=%s check_only=%s" % (sum(1 for item in changed if item), args.check))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
