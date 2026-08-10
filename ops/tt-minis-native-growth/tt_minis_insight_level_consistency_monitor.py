#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Monitor TT minis TikTok insight consistency across campaign/adgroup/ad levels."""

import argparse
import csv
import json
import os
import subprocess
import urllib.parse
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import opera_product_daily_dashboard as base
import tt_minis_multi_dim_dashboard as dash


ROOT = Path("/root/codex_test")
STATE_FILE = ROOT / "state" / "tt_minis_insight_level_consistency_state.json"
PUBLISHED_REPORT_ROOT = Path("/usr/share/nginx/html/reports/tt-minis-native-growth")
DEFAULT_CHAT_ID = "oc_03f5e06ca6b380843aab91fec372f3fe"
BJ_OFFSET_HOURS = 8
MAX_SNAPSHOT_AGE = timedelta(hours=2)
LEVELS = {
    "campaign": {"category": 0, "label": "campaign层级"},
    "adgroup": {"category": 1, "label": "adgroup层级"},
    "ad": {"category": 2, "label": "ad层级"},
}
LEVEL_KEY = {"campaign": "campaign_id", "adgroup": "adset_id", "ad": "ad_id"}
INSIGHT_KEY = {"campaign": "campaign_id", "adgroup": "adgroup_id", "ad": "ad_id"}
UNAVAILABLE = {"", "未填", "campaign层级不可用", None}


def bj_now():
    return datetime.utcnow() + timedelta(hours=BJ_OFFSET_HOURS)


def dec(value):
    if value in (None, "", "NULL"):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def money(value):
    return "$" + format(dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ",")


def pct(value):
    return str((dec(value) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) + "%"


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_snapshot_path(report_root, relative_path):
    report_root = Path(report_root).resolve()
    path = (report_root / relative_path).resolve()
    if path != report_root and report_root not in path.parents:
        raise RuntimeError("snapshot path escapes the published report root")
    return path


def decode_snapshot_rows(payload):
    columns = payload.get("columns") or []
    dictionary_columns = set(payload.get("dict_columns") or [])
    dictionaries = payload.get("dicts") or {}
    rows = []
    for compact in payload.get("rows") or []:
        if isinstance(compact, dict):
            rows.append(compact)
            continue
        if not isinstance(compact, list) or len(compact) != len(columns):
            raise RuntimeError("invalid compact row in published snapshot")
        row = {}
        for column, value in zip(columns, compact):
            if column in dictionary_columns:
                values = dictionaries.get(column) or []
                if not isinstance(value, int) or value < 0 or value >= len(values):
                    raise RuntimeError("invalid dictionary value for %s" % column)
                value = values[value]
            row[column] = value
        rows.append(row)
    return rows


def validate_manifest_freshness(manifest, max_snapshot_age=MAX_SNAPSHOT_AGE):
    generated_text = str((manifest.get("meta") or {}).get("generated_at") or "")
    try:
        generated_at = datetime.strptime(generated_text, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise RuntimeError("published dashboard snapshot has no valid generated_at") from exc
    age = bj_now() - generated_at
    if age < timedelta(minutes=-5) or (max_snapshot_age is not None and age > max_snapshot_age):
        raise RuntimeError("published dashboard snapshot is stale: generated_at=%s" % generated_text)
    return generated_text


def load_published_scope_contexts(days, report_root=PUBLISHED_REPORT_ROOT, max_snapshot_age=MAX_SNAPSHOT_AGE):
    """Load compact campaign scope contexts from one atomically selected manifest."""
    report_root = Path(report_root)
    manifest = load_json(report_root / "latest.json")
    generated_text = validate_manifest_freshness(manifest, max_snapshot_age=max_snapshot_age)
    ad_files = ((manifest.get("data_files") or {}).get("ad") or {})
    result = {}
    for day in days:
        file_meta = ad_files.get(day)
        if not file_meta or not file_meta.get("path"):
            raise RuntimeError("published ad-level snapshot is missing date %s" % day)
        payload = load_json(resolve_snapshot_path(report_root, file_meta["path"]))
        meta = payload.get("meta") or {}
        if meta.get("metric_level") != "ad" or meta.get("start_date") != day or meta.get("end_date") != day:
            raise RuntimeError("published ad-level snapshot metadata mismatch for %s" % day)
        rows = decode_snapshot_rows(payload)
        expected_count = int(file_meta.get("row_count") or 0)
        if expected_count != len(rows) or int(meta.get("row_count") or 0) != len(rows):
            raise RuntimeError("published ad-level snapshot row count mismatch for %s" % day)
        result[day] = scope_context_from_ad_rows(rows)
    return result, generated_text


def numeric_id(value):
    text = str(value or "").strip()
    return str(int(text)) if text not in UNAVAILABLE and text.isdigit() and int(text) > 0 else ""


def scope_from_ad_rows(rows, key):
    return {value for value in (numeric_id(row.get(key)) for row in rows) if value}


def scope_context_from_ad_rows(rows):
    id_keys = ("campaign_id", "adset_id", "ad_id")
    return {
        "campaign_ids": scope_from_ad_rows(rows, "campaign_id"),
        "snapshot_adgroup_count": len(scope_from_ad_rows(rows, "adset_id")),
        "snapshot_ad_count": len(scope_from_ad_rows(rows, "ad_id")),
        "snapshot_noncanonical_id_count_by_field": {
            key: sum(
                1
                for row in rows
                if numeric_id(row.get(key))
                and str(row.get(key)).strip() != numeric_id(row.get(key))
            )
            for key in id_keys
        },
    }


def live_level_rollup_sql(day):
    """Return all three levels in one exact-day consistent-read statement."""
    branches = []
    for level, cfg in LEVELS.items():
        scope_key = INSIGHT_KEY[level]
        branches.append(
            f"""
            SELECT
              {dash.sql_quote(level)} AS metric_level,
              CAST({scope_key} AS UNSIGNED) AS scope_id,
              CAST(campaign_id AS CHAR) AS campaign_id,
              CAST(adgroup_id AS CHAR) AS adgroup_id,
              CAST(ad_id AS CHAR) AS ad_id,
              COUNT(*) AS insight_rows,
              ROUND(SUM(stat_cost), 6) AS spend,
              ROUND(SUM(ad_impression_value), 6) AS revenue
            FROM kunlunads_dev.ads_tiktok_insights FORCE INDEX(pcsa)
            WHERE product IN {dash.sql_in(dash.TIKTOK_INSIGHT_PRODUCTS)}
              AND start_date = {dash.sql_quote(day)}
              AND category = {cfg["category"]}
              AND {scope_key} IS NOT NULL
              AND TRIM({scope_key}) <> ''
              AND {scope_key} REGEXP '^[0-9]+$'
              AND CAST({scope_key} AS UNSIGNED) > 0
            GROUP BY
              CAST({scope_key} AS UNSIGNED),
              CAST(campaign_id AS CHAR),
              CAST(adgroup_id AS CHAR),
              CAST(ad_id AS CHAR)
            """
        )
    return "\nUNION ALL\n".join(branches)


def mysql_command_and_env():
    """Move an inline mysql password to MYSQL_PWD before spawning the client."""
    raw = list(base.mysql_cmd())
    command = []
    password = None
    skip_next = False
    for index, arg in enumerate(raw):
        if skip_next:
            skip_next = False
            continue
        text = str(arg)
        if text in ("-p", "--password") and index + 1 < len(raw):
            password = str(raw[index + 1])
            skip_next = True
        elif text.startswith("-p") and len(text) > 2:
            password = text[2:]
        elif text.startswith("--password="):
            password = text.split("=", 1)[1]
        else:
            command.append(text)
    env = os.environ.copy()
    if password is not None:
        env["MYSQL_PWD"] = password
    return command, env


def run_mysql_safe(sql, timeout=120):
    """Run MySQL without putting its password in argv or exception text."""
    try:
        command, env = mysql_command_and_env()
        compact_sql = base.compact_sql(sql)
        proc = subprocess.run(
            command + [compact_sql],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("TT minis consistency query timed out after %s seconds" % timeout) from None
    except Exception as exc:
        raise RuntimeError("TT minis consistency query failed before completion (%s)" % type(exc).__name__) from None
    if proc.returncode:
        raise RuntimeError("TT minis consistency query failed with mysql exit code %s" % proc.returncode)
    return list(csv.reader(proc.stdout.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE))


def fetch_live_levels(day, scope_context):
    campaign_scope = scope_context["campaign_ids"]
    if not campaign_scope:
        raise RuntimeError("published ad-level snapshot has no campaign scope for %s" % day)
    rows = run_mysql_safe(live_level_rollup_sql(day), timeout=120)
    accumulators = {
        level: {
            "rows": Decimal("0"),
            "campaigns": set(),
            "adgroups": set(),
            "ads": set(),
            "spend": Decimal("0"),
            "revenue": Decimal("0"),
        }
        for level in LEVELS
    }
    returned_groups = 0
    returned_insight_rows = Decimal("0")
    missing_parent_groups = 0
    missing_parent_insight_rows = Decimal("0")
    ignored_foreign_rows = Decimal("0")
    ignored_foreign_groups = 0
    matched_groups = 0
    for raw_row in rows:
        returned_groups += 1
        row = raw_row
        if len(row) != 8:
            raise RuntimeError("TT minis consistency query returned an invalid column count")
        level = str(row[0] or "").strip()
        if level not in LEVELS:
            raise RuntimeError("TT minis consistency query returned an invalid metric level")
        parsed = {
            "level": level,
            "scope_id": numeric_id(row[1]),
            "campaign_id": numeric_id(row[2]),
            "adgroup_id": numeric_id(row[3]),
            "ad_id": numeric_id(row[4]),
            "rows": dec(row[5]),
            "spend": dec(row[6]),
            "revenue": dec(row[7]),
        }
        returned_insight_rows += parsed["rows"]
        if parsed["level"] in ("adgroup", "ad") and parsed["scope_id"] and not parsed["campaign_id"]:
            missing_parent_groups += 1
            missing_parent_insight_rows += parsed["rows"]
            continue
        campaign_id = parsed["campaign_id"]
        if not campaign_id or campaign_id not in campaign_scope:
            ignored_foreign_rows += parsed["rows"]
            ignored_foreign_groups += 1
            continue
        matched_groups += 1
        accumulator = accumulators[level]
        accumulator["campaigns"].add(campaign_id)
        if parsed["adgroup_id"]:
            accumulator["adgroups"].add(parsed["adgroup_id"])
        if parsed["ad_id"]:
            accumulator["ads"].add(parsed["ad_id"])
        accumulator["rows"] += parsed["rows"]
        accumulator["spend"] += parsed["spend"]
        accumulator["revenue"] += parsed["revenue"]
    if missing_parent_groups:
        raise RuntimeError(
            "TT minis consistency query returned child rows without campaign_id: groups=%s rows=%s"
            % (missing_parent_groups, int(missing_parent_insight_rows))
        )
    if not matched_groups:
        raise RuntimeError("TT minis consistency query matched no live groups for scoped campaigns")
    metrics = {
        level: {
            "rows": int(accumulator["rows"]),
            "campaigns": len(accumulator["campaigns"]),
            "adgroups": len(accumulator["adgroups"]),
            "ads": len(accumulator["ads"]),
            "spend": accumulator["spend"],
            "revenue": accumulator["revenue"],
        }
        for level, accumulator in accumulators.items()
    }
    campaigns_seen_anywhere = set().union(
        *(accumulators[level]["campaigns"] for level in LEVELS)
    )
    diagnostics = {
        "scope_mode": "snapshot_campaign_closed_live_children",
        "snapshot_campaign_count": len(campaign_scope),
        "snapshot_adgroup_count": scope_context["snapshot_adgroup_count"],
        "snapshot_ad_count": scope_context["snapshot_ad_count"],
        "snapshot_noncanonical_id_count_by_field": scope_context["snapshot_noncanonical_id_count_by_field"],
        "live_group_rows_returned": returned_groups,
        "live_insight_rows_returned": int(returned_insight_rows),
        "matched_live_group_rows": matched_groups,
        "matched_live_insight_rows": int(
            sum((accumulator["rows"] for accumulator in accumulators.values()), Decimal("0"))
        ),
        "ignored_foreign_group_rows": ignored_foreign_groups,
        "ignored_foreign_insight_rows": int(ignored_foreign_rows),
        "missing_parent_insight_rows": 0,
        "missing_scoped_campaign_count_by_level": {
            level: len(campaign_scope - accumulators[level]["campaigns"])
            for level in LEVELS
        },
        "missing_scoped_campaign_count_all_levels": len(campaign_scope - campaigns_seen_anywhere),
    }
    return metrics, diagnostics


def fetch_days(days, report_root=PUBLISHED_REPORT_ROOT, max_snapshot_age=MAX_SNAPSHOT_AGE):
    scope_contexts, generated_at = load_published_scope_contexts(
        days,
        report_root=report_root,
        max_snapshot_age=max_snapshot_age,
    )
    result = {}
    diagnostics = {}
    for day in days:
        result[day], diagnostics[day] = fetch_live_levels(day, scope_contexts[day])
    return result, generated_at, diagnostics


def is_large_diff(a, b, abs_threshold, pct_threshold):
    diff = abs(dec(a) - dec(b))
    base_value = max(abs(dec(a)), abs(dec(b)), Decimal("1"))
    return diff >= abs_threshold and (diff / base_value) >= pct_threshold


def classify_metric(day, metric, values, abs_threshold, pct_threshold):
    levels = list(LEVELS)
    bad_pairs = []
    for index, left in enumerate(levels):
        for right in levels[index + 1 :]:
            if is_large_diff(values[left], values[right], abs_threshold, pct_threshold):
                bad_pairs.append((left, right))
    if not bad_pairs:
        return None
    pair_counts = {level: 0 for level in levels}
    for left, right in bad_pairs:
        pair_counts[left] += 1
        pair_counts[right] += 1
    abnormal = [level for level, count in pair_counts.items() if count == 2]
    if len(abnormal) == 1 and len(bad_pairs) == 2:
        abnormal_level = abnormal[0]
        label = LEVELS[abnormal_level]["label"] + "异常"
    else:
        abnormal_level = "multi"
        label = "多层级不一致，需人工确认"
    max_level = max(levels, key=lambda level: values[level])
    min_level = min(levels, key=lambda level: values[level])
    diff = values[max_level] - values[min_level]
    diff_pct = abs(diff) / max(abs(values[max_level]), Decimal("1"))
    return {
        "key": "%s:%s" % (day, metric),
        "day": day,
        "metric": metric,
        "abnormal_level": abnormal_level,
        "label": label,
        "values": {level: str(values[level]) for level in levels},
        "max_level": max_level,
        "min_level": min_level,
        "diff": str(diff),
        "diff_pct": str(diff_pct),
    }


def find_anomalies(day, day_data, abs_threshold, pct_threshold):
    anomalies = []
    for metric in ("spend", "revenue"):
        values = {level: day_data[level][metric] for level in LEVELS}
        item = classify_metric(day, metric, values, abs_threshold, pct_threshold)
        if item:
            anomalies.append(item)
    return anomalies


def load_state():
    if not STATE_FILE.exists():
        return {"active": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"active": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def changed_alerts(anomalies, state):
    active = state.get("active") or {}
    current = {item["key"]: item for item in anomalies}
    alerts = []
    recoveries = []
    for key, item in current.items():
        old = active.get(key)
        if not old or old.get("abnormal_level") != item.get("abnormal_level"):
            alerts.append(item)
    for key, old in active.items():
        if key not in current:
            recoveries.append(old)
    state["active"] = current
    state["updated_at"] = bj_now().strftime("%Y-%m-%d %H:%M:%S")
    return alerts, recoveries


def metric_label(metric):
    return {"spend": "消耗", "revenue": "收入"}.get(metric, metric)


def metric_value(metric, value):
    return money(value) if metric in ("spend", "revenue") else str(value)


def notification_presentation(alerts, recoveries):
    timestamp = bj_now().strftime("%m-%d %H:%M")
    if recoveries and not alerts:
        return "TT小程序三层级消耗收入异常已恢复｜%s" % timestamp, "green"
    return "TT小程序三层级消耗收入异常｜%s" % timestamp, "red"


def build_markdown(alerts, recoveries, all_data, abs_threshold, pct_threshold):
    lines = [
        "**口径**：TT小程序广告；`minis_id=mn1yi38ikcrqhitt`，`product_id IN (1479,3346)`；原子日快照只锁定 campaign 范围，同一 campaign 的全部实时子级由同一条 `UNION ALL` SQL 读取。",
        "**阈值**：绝对差 >= %s 且相对差 >= %s。" % (money(abs_threshold), pct(pct_threshold)),
    ]
    if alerts:
        lines.append("\n**异常明细**")
        for item in alerts:
            metric = item["metric"]
            values = item["values"]
            lines.append(
                "- {day}｜{metric}｜{label}｜campaign {campaign}｜adgroup {adgroup}｜ad {ad}｜最大差 {diff}（{diff_pct}）".format(
                    day=item["day"],
                    metric=metric_label(metric),
                    label=item["label"],
                    campaign=metric_value(metric, values["campaign"]),
                    adgroup=metric_value(metric, values["adgroup"]),
                    ad=metric_value(metric, values["ad"]),
                    diff=metric_value(metric, item["diff"]),
                    diff_pct=pct(item["diff_pct"]),
                )
            )
    if recoveries:
        lines.append("\n**恢复提示**")
        for item in recoveries:
            lines.append("- %s｜%s｜此前 %s 已恢复到阈值内" % (item["day"], metric_label(item["metric"]), item["label"]))
    lines.append("\n**本次三层级快照**")
    for day in sorted(all_data.keys(), reverse=True):
        day_data = all_data[day]
        lines.append(
            "- {day}｜消耗 campaign {c_spend} / adgroup {g_spend} / ad {a_spend}｜收入 campaign {c_rev} / adgroup {g_rev} / ad {a_rev}".format(
                day=day,
                c_spend=money(day_data["campaign"]["spend"]),
                g_spend=money(day_data["adgroup"]["spend"]),
                a_spend=money(day_data["ad"]["spend"]),
                c_rev=money(day_data["campaign"]["revenue"]),
                g_rev=money(day_data["adgroup"]["revenue"]),
                a_rev=money(day_data["ad"]["revenue"]),
            )
        )
    return "\n".join(lines)


def send_feishu_card(receive_id, title, markdown, header_template="red"):
    domain, token = base.feishu_token()
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": header_template, "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": markdown}}],
    }
    return base.post_json(
        domain + "/open-apis/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": "chat_id"}),
        {"receive_id": receive_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        headers={"Authorization": "Bearer " + token},
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="", help="Base date, default today Asia/Shanghai.")
    parser.add_argument("--days", type=int, default=2, help="Check today plus previous days. Default 2 means today+yesterday.")
    parser.add_argument("--abs-threshold", default="100")
    parser.add_argument("--pct-threshold", default="0.05")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dry-run-message", action="store_true")
    parser.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    parser.add_argument("--snapshot-root", default=str(PUBLISHED_REPORT_ROOT), help=argparse.SUPPRESS)
    return parser.parse_args()


def main():
    args = parse_args()
    end_day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else bj_now().date()
    days = [(end_day - timedelta(days=offset)).isoformat() for offset in range(max(1, args.days))]
    abs_threshold = dec(args.abs_threshold)
    pct_threshold = dec(args.pct_threshold)
    all_data, snapshot_generated_at, scope_diagnostics = fetch_days(days, report_root=Path(args.snapshot_root))
    anomalies = []
    for day, day_data in all_data.items():
        anomalies.extend(find_anomalies(day, day_data, abs_threshold, pct_threshold))
    state = load_state()
    alerts, recoveries = changed_alerts(anomalies, state)
    markdown = build_markdown(alerts, recoveries, all_data, abs_threshold, pct_threshold)
    title, header_template = notification_presentation(alerts, recoveries)
    result = {
        "title": title,
        "header_template": header_template,
        "days": days,
        "snapshot_generated_at": snapshot_generated_at,
        "scope_diagnostics": scope_diagnostics,
        "all_data": {
            day: {level: {key: str(value) for key, value in metrics.items()} for level, metrics in day_data.items()}
            for day, day_data in all_data.items()
        },
        "anomalies": anomalies,
        "alerts": alerts,
        "recoveries": recoveries,
        "markdown": markdown if args.dry_run_message else "",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.send and (alerts or recoveries):
        response = send_feishu_card(args.chat_id, title, markdown, header_template=header_template)
        if not isinstance(response, dict) or response.get("code") != 0:
            code = response.get("code") if isinstance(response, dict) else None
            raise RuntimeError("Feishu send failed: code=%s" % code)
        print("Feishu sent message_id=%s" % ((response.get("data") or {}).get("message_id", "")))
    if not args.dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
