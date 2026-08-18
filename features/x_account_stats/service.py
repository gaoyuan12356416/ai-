"""Build and serve cached X account operating statistics.

The refresh path reads the X SQLite ledgers and the gated read-only MySQL
client.  Web requests only read the resulting JSON snapshot.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import json
import os
import sqlite3
import subprocess
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
SITE_ID = "2116"
BUSINESS_TIMEZONE = "Asia/Shanghai"
SHANGHAI = ZoneInfo(BUSINESS_TIMEZONE)
DEFAULT_CACHE_ROOT = Path("/mnt/data-disk/x-account-operating-stats")
DEFAULT_CACHE_PATH = DEFAULT_CACHE_ROOT / "current.json"
DEFAULT_MAX_AGE_SECONDS = 15 * 60 * 60
DEFAULT_MAX_FUTURE_SKEW_SECONDS = 5 * 60
APPROVED_MYSQL_ENTRY = Path("/usr/bin/mysql")
APPROVED_MYSQL_TARGET = Path("/usr/local/bin/mysql-gated")
ZERO = Decimal("0")


class StatsRefreshError(RuntimeError):
    """Secret-safe refresh failure."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_beijing_date(value: object, expected: date) -> bool:
    parsed = _parse_utc(value)
    return bool(parsed and parsed.astimezone(SHANGHAI).date() == expected)


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        raise StatsRefreshError("收入数据包含无效金额") from None
    if not parsed.is_finite():
        raise StatsRefreshError("收入数据包含无效金额")
    return parsed


def _money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def campaign_from_long_url(long_url: object) -> str:
    """Return the one exact frozen ``c`` value, otherwise an empty string."""

    try:
        parsed = urlsplit(str(long_url or ""))
    except ValueError:
        return ""
    values = [value for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key == "c"]
    if len(values) != 1:
        return ""
    return values[0]


def _open_ledger(path: os.PathLike[str] | str) -> sqlite3.Connection:
    resolved = Path(path).resolve(strict=True)
    conn = sqlite3.connect(
        "file:%s?mode=ro" % resolved.as_posix(), uri=True, timeout=30
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def read_ledger_metrics(
    db_path: os.PathLike[str] | str, yesterday: date
) -> tuple[dict[int, dict[str, int]], dict[str, int], dict[str, int]]:
    """Read actor counts and the exact campaign-to-target-account mapping."""

    metrics: dict[int, dict[str, int]] = defaultdict(
        lambda: {
            "published_posts_total": 0,
            "published_posts_yesterday": 0,
            "reposts_total": 0,
            "reposts_yesterday": 0,
        }
    )
    campaign_accounts: dict[str, set[int]] = defaultdict(set)
    campaign_evidence = {
        "campaigns": 0,
        "conflicts": 0,
        "missing": 0,
        "unconfirmed": 0,
        "ledger_conflicts": 0,
    }
    with contextlib.closing(_open_ledger(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT q.account_id,q.delivery_mode,l.status,l.x_post_id,
                   l.published_at,l.long_url
            FROM x_post_queue q
            JOIN x_post_publish_log l ON l.queue_id=q.id
            """
        ).fetchall()
        for row in rows:
            account_id = int(row["account_id"])
            confirmed = (
                str(row["status"] or "") == "published"
                and bool(str(row["x_post_id"] or ""))
            )
            if confirmed:
                campaign = campaign_from_long_url(row["long_url"])
                if campaign:
                    campaign_accounts[campaign].add(account_id)
                else:
                    campaign_evidence["missing"] += 1
            else:
                campaign_evidence["unconfirmed"] += 1
            if (
                str(row["delivery_mode"] or "direct") == "direct"
                and confirmed
            ):
                metrics[account_id]["published_posts_total"] += 1
                if _is_beijing_date(row["published_at"], yesterday):
                    metrics[account_id]["published_posts_yesterday"] += 1

        relay_rows = conn.execute(
            """
            SELECT r.relay_account_id AS ledger_relay_account_id,
                   r.target_account_id AS ledger_target_account_id,
                   r.status,r.source_post_id,r.source_published_at,r.reposted_at,
                   q.id AS queue_id,q.account_id AS queue_target_account_id,
                   q.relay_account_id AS queue_relay_account_id,
                   q.delivery_mode AS queue_delivery_mode
            FROM x_post_repost_ledger r
            LEFT JOIN x_post_queue q ON q.id=r.queue_id
            """
        ).fetchall()
        for row in relay_rows:
            consistent = (
                row["queue_id"] is not None
                and str(row["queue_delivery_mode"] or "")
                == "premium_relay_repost"
                and int(row["queue_relay_account_id"] or 0) > 0
                and int(row["queue_relay_account_id"] or 0)
                == int(row["ledger_relay_account_id"] or 0)
                and int(row["queue_target_account_id"] or 0)
                == int(row["ledger_target_account_id"] or 0)
            )
            if not consistent:
                campaign_evidence["ledger_conflicts"] += 1
                continue
            if str(row["source_post_id"] or "") and _parse_utc(
                row["source_published_at"]
            ):
                relay_id = int(row["queue_relay_account_id"])
                metrics[relay_id]["published_posts_total"] += 1
                if _is_beijing_date(row["source_published_at"], yesterday):
                    metrics[relay_id]["published_posts_yesterday"] += 1
            if str(row["status"] or "") == "reposted":
                target_id = int(row["queue_target_account_id"])
                metrics[target_id]["reposts_total"] += 1
                if _is_beijing_date(row["reposted_at"], yesterday):
                    metrics[target_id]["reposts_yesterday"] += 1

    exact_campaigns: dict[str, int] = {}
    for campaign, account_ids in campaign_accounts.items():
        if len(account_ids) == 1:
            exact_campaigns[campaign] = next(iter(account_ids))
        else:
            campaign_evidence["conflicts"] += 1
    campaign_evidence["campaigns"] = len(exact_campaigns)
    return dict(metrics), exact_campaigns, campaign_evidence


def revenue_query(yesterday: date) -> str:
    """Return the fixed-site, Beijing-session aggregate query."""

    day = yesterday.isoformat()
    campaign_binary = "CONVERT(COALESCE(campaign,'') USING binary)"
    return f"""SET SESSION time_zone = '+08:00';
SELECT REPLACE(TO_BASE64({campaign_binary}),CHAR(10),''),
       CAST(COALESCE(SUM(event_revenue_usd),0) AS CHAR),
       CAST(COALESCE(SUM(CASE WHEN DATE(FROM_UNIXTIME(event_time))='{day}'
            THEN event_revenue_usd ELSE 0 END),0) AS CHAR)
FROM ads_drama_bills FORCE INDEX(idx_site_event_time)
WHERE site_id='2116'
GROUP BY {campaign_binary}
ORDER BY {campaign_binary};
"""


def assert_approved_mysql_entry(
    mysql_entry: str = "/usr/bin/mysql", *, resolver=None
) -> Path:
    """Require the host-gated entry and its exact approved resolved target."""

    entry = Path(str(mysql_entry))
    if entry != APPROVED_MYSQL_ENTRY:
        raise StatsRefreshError("MySQL客户端必须使用宿主受控入口 /usr/bin/mysql")
    try:
        resolved = Path(resolver(entry) if resolver else entry.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        raise StatsRefreshError("无法验证宿主 SQL gate") from None
    if resolved != APPROVED_MYSQL_TARGET:
        raise StatsRefreshError("宿主 SQL gate 入口不符合批准配置")
    return resolved


def run_gated_mysql(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    query: str,
    mysql_binary: str = "/usr/bin/mysql",
    connect_timeout: int = 10,
    query_timeout: int = 300,
) -> list[tuple[str, Decimal, Decimal]]:
    """Run the host-gated mysql client; the password is environment-only."""

    if str(mysql_binary) != "/usr/bin/mysql":
        raise StatsRefreshError("MySQL客户端必须使用宿主受控入口 /usr/bin/mysql")
    if not host or not user or not database or not password:
        raise StatsRefreshError("只读收入数据库配置不完整")
    command = [
        "/usr/bin/mysql",
        "--batch",
        "--raw",
        "--skip-column-names",
        "--default-character-set=utf8mb4",
        "--connect-timeout=%d" % max(1, min(int(connect_timeout), 60)),
        "--host=%s" % host,
        "--port=%d" % int(port),
        "--user=%s" % user,
        database,
    ]
    child_env = {"MYSQL_PWD": password}
    for name in ("PATH", "LANG", "LC_ALL", "TZ", "HOME"):
        if name in os.environ:
            child_env[name] = os.environ[name]
    try:
        completed = subprocess.run(
            command,
            input=query,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            timeout=max(1, min(int(query_timeout), 900)),
            check=False,
            env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise StatsRefreshError("受控只读收入查询失败") from None
    if completed.returncode != 0:
        raise StatsRefreshError("受控只读收入查询失败")
    if len(completed.stdout.encode("utf-8")) > 64 * 1024 * 1024:
        raise StatsRefreshError("收入查询结果超过安全上限")
    result: list[tuple[str, Decimal, Decimal]] = []
    for line in completed.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise StatsRefreshError("收入查询结果格式无效")
        try:
            campaign = base64.b64decode(fields[0], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise StatsRefreshError("收入查询结果格式无效") from None
        result.append((campaign, _decimal(fields[1]), _decimal(fields[2])))
    return result


def build_snapshot(
    *,
    ledger_metrics: dict[int, dict[str, int]],
    campaign_accounts: dict[str, int],
    campaign_evidence: dict[str, int],
    revenue_rows: list[tuple[str, Decimal, Decimal]],
    now: datetime,
) -> dict[str, object]:
    now = now.astimezone(timezone.utc)
    business_date = now.astimezone(SHANGHAI).date()
    yesterday = business_date - timedelta(days=1)
    accounts: dict[str, dict[str, object]] = {}
    for account_id, counts in ledger_metrics.items():
        accounts[str(account_id)] = {
            **counts,
            "revenue_total_usd": _money_text(ZERO),
            "revenue_yesterday_usd": _money_text(ZERO),
        }
    unallocated_total = ZERO
    unallocated_yesterday = ZERO
    allocated_campaigns = 0
    unallocated_campaigns = 0
    for campaign, total, day_total in revenue_rows:
        account_id = campaign_accounts.get(campaign)
        if account_id is None:
            unallocated_total += total
            unallocated_yesterday += day_total
            unallocated_campaigns += 1
            continue
        item = accounts.setdefault(
            str(account_id),
            {
                "published_posts_total": 0,
                "published_posts_yesterday": 0,
                "reposts_total": 0,
                "reposts_yesterday": 0,
                "revenue_total_usd": _money_text(ZERO),
                "revenue_yesterday_usd": _money_text(ZERO),
            },
        )
        item["revenue_total_usd"] = _money_text(
            _decimal(item["revenue_total_usd"]) + total
        )
        item["revenue_yesterday_usd"] = _money_text(
            _decimal(item["revenue_yesterday_usd"]) + day_total
        )
        allocated_campaigns += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "site_id": SITE_ID,
        "timezone": BUSINESS_TIMEZONE,
        "generated_at_utc": _utc_text(now),
        "business_date": business_date.isoformat(),
        "yesterday_date": yesterday.isoformat(),
        "accounts": accounts,
        "unallocated_revenue": {
            "total_usd": _money_text(unallocated_total),
            "yesterday_usd": _money_text(unallocated_yesterday),
        },
        "attribution": {
            **campaign_evidence,
            "allocated_revenue_campaigns": allocated_campaigns,
            "unallocated_revenue_campaigns": unallocated_campaigns,
        },
    }


def _assert_cache_path(path: Path, root: Path) -> tuple[Path, Path]:
    root_resolved = root.resolve(strict=True)
    path_parent = path.parent.resolve(strict=True)
    if path_parent != root_resolved and root_resolved not in path_parent.parents:
        raise StatsRefreshError("统计缓存路径不在已验证数据盘目录")
    return path_parent / path.name, root_resolved


def write_snapshot_atomic(
    snapshot: dict[str, object],
    cache_path: os.PathLike[str] | str = DEFAULT_CACHE_PATH,
    cache_root: os.PathLike[str] | str = DEFAULT_CACHE_ROOT,
) -> None:
    path, _root = _assert_cache_path(Path(cache_path), Path(cache_root))
    payload = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(payload) > 16 * 1024 * 1024:
        raise StatsRefreshError("统计缓存超过安全上限")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_snapshot(
    cache_path: os.PathLike[str] | str,
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    max_future_skew_seconds: int = DEFAULT_MAX_FUTURE_SKEW_SECONDS,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    now = (now or utc_now()).astimezone(timezone.utc)
    try:
        raw = Path(cache_path).read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("oversize")
        snapshot = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("schema_version") != SCHEMA_VERSION
            or snapshot.get("site_id") != SITE_ID
            or not isinstance(snapshot.get("accounts"), dict)
        ):
            raise ValueError("invalid cache")
        generated = _parse_utc(snapshot.get("generated_at_utc"))
        if not generated:
            raise ValueError("invalid generated time")
        business_date_raw = snapshot.get("business_date")
        yesterday_date_raw = snapshot.get("yesterday_date")
        if not isinstance(business_date_raw, str) or not isinstance(
            yesterday_date_raw, str
        ):
            raise ValueError("invalid business dates")
        business_date = date.fromisoformat(business_date_raw)
        yesterday_date = date.fromisoformat(yesterday_date_raw)
        if (
            business_date.isoformat() != business_date_raw
            or yesterday_date.isoformat() != yesterday_date_raw
            or yesterday_date != business_date - timedelta(days=1)
        ):
            raise ValueError("invalid business dates")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, {
            "status": "missing",
            "available": False,
            "stale": False,
            "generated_at_utc": "",
            "message": "运营统计缓存尚未生成，请等待定时刷新",
        }
    expected_business_date = now.astimezone(SHANGHAI).date().isoformat()
    age_delta = (now - generated).total_seconds()
    age_seconds = max(0, int(age_delta))
    stale_reasons = []
    if age_seconds > max(1, int(max_age_seconds)):
        stale_reasons.append("age")
    if business_date.isoformat() != expected_business_date:
        stale_reasons.append("business_date")
    if age_delta < -max(1, int(max_future_skew_seconds)):
        stale_reasons.append("future_generated_at")
    stale = bool(stale_reasons)
    return snapshot, {
        "status": "stale" if stale else "fresh",
        "available": True,
        "stale": stale,
        "generated_at_utc": snapshot["generated_at_utc"],
        "business_date": snapshot.get("business_date", ""),
        "yesterday_date": snapshot.get("yesterday_date", ""),
        "age_seconds": age_seconds,
        "stale_reasons": stale_reasons,
        "message": "运营统计缓存已过期，请检查定时刷新" if stale else "运营统计已更新",
    }


def merge_account_stats(
    payload: dict[str, object],
    cache_path: os.PathLike[str] | str,
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    max_future_skew_seconds: int = DEFAULT_MAX_FUTURE_SKEW_SECONDS,
) -> dict[str, object]:
    """Return a new admin DTO with cache-only operating metrics."""

    result = copy.deepcopy(payload if isinstance(payload, dict) else {})
    snapshot, meta = read_snapshot(
        cache_path,
        now=now,
        max_age_seconds=max_age_seconds,
        max_future_skew_seconds=max_future_skew_seconds,
    )
    cached_accounts = snapshot.get("accounts", {}) if snapshot else {}
    empty = {
        "published_posts_total": None,
        "published_posts_yesterday": None,
        "reposts_total": None,
        "reposts_yesterday": None,
        "revenue_total_usd": None,
        "revenue_yesterday_usd": None,
    }
    zero = {
        "published_posts_total": 0,
        "published_posts_yesterday": 0,
        "reposts_total": 0,
        "reposts_yesterday": 0,
        "revenue_total_usd": "0.000000",
        "revenue_yesterday_usd": "0.000000",
    }
    for item in result.get("items", []):
        if not isinstance(item, dict):
            continue
        account_stats = cached_accounts.get(str(item.get("id", "")))
        if isinstance(account_stats, dict):
            item["operating_stats"] = dict(account_stats)
        else:
            item["operating_stats"] = dict(zero if snapshot else empty)
    if snapshot:
        meta["unallocated_revenue"] = snapshot.get(
            "unallocated_revenue",
            {"total_usd": "0.000000", "yesterday_usd": "0.000000"},
        )
        meta["attribution"] = snapshot.get("attribution", {})
    else:
        meta["unallocated_revenue"] = {
            "total_usd": None,
            "yesterday_usd": None,
        }
        meta["attribution"] = {}
    result["operating_stats_meta"] = meta
    return result
