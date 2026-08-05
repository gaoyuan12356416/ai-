#!/usr/bin/env python3
"""Refresh TT automatic-post metric cache one complete Beijing day at a time."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_auto_posts.repositories import (  # noqa: E402
    DEFAULT_BLACKLIST_SCHEMA,
    DEFAULT_PLATFORM,
    DEFAULT_PRODUCT,
    DEFAULT_SCHEMA,
    ReadOnlyMySQLRepository,
    complete_beijing_dates,
    normalize_metric_date,
    refresh_metric_day,
)
from features.x_posts.selector import connect_read_only  # noqa: E402


UTC = timezone.utc
DEFAULT_DB_PATH = "/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3"
DEFAULT_MYSQL_HOST = "101.32.56.53"
DEFAULT_MYSQL_PORT = 63350
DEFAULT_LOCK_PATH = "/run/tt-auto-post/metric.lock"


@dataclass(frozen=True)
class MetricRunnerConfig:
    db_path: str
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    blacklist_database: str
    product: str
    platform: int
    lookback_days: int
    lock_path: str

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "MetricRunnerConfig":
        source = os.environ if environ is None else environ
        try:
            port = int(
                source.get(
                    "TT_AUTO_POST_MYSQL_PORT",
                    source.get("TT_POST_MYSQL_PORT", DEFAULT_MYSQL_PORT),
                )
            )
            platform = int(source.get("TT_AUTO_POST_METRIC_PLATFORM", DEFAULT_PLATFORM))
            lookback = int(source.get("TT_AUTO_POST_METRIC_LOOKBACK_DAYS", "7"))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("metric runner numeric configuration is invalid") from None
        config = cls(
            db_path=str(source.get("TT_AUTO_POST_DB_PATH", DEFAULT_DB_PATH)).strip(),
            mysql_host=str(
                source.get(
                    "TT_AUTO_POST_MYSQL_HOST",
                    source.get("TT_POST_MYSQL_HOST", DEFAULT_MYSQL_HOST),
                )
            ).strip(),
            mysql_port=port,
            mysql_user=str(
                source.get(
                    "TT_AUTO_POST_MYSQL_USER",
                    source.get("TT_POST_MYSQL_USER", ""),
                )
            ).strip(),
            mysql_password=str(
                source.get(
                    "TT_AUTO_POST_MYSQL_PASSWORD",
                    source.get("TT_POST_MYSQL_PASSWORD", ""),
                )
            ),
            mysql_database=str(
                source.get(
                    "TT_AUTO_POST_MYSQL_DATABASE",
                    source.get("TT_POST_MATERIAL_MYSQL_DATABASE", DEFAULT_SCHEMA),
                )
            ).strip(),
            blacklist_database=str(
                source.get(
                    "TT_AUTO_POST_BLACKLIST_DATABASE",
                    DEFAULT_BLACKLIST_SCHEMA,
                )
            ).strip(),
            product=str(source.get("TT_AUTO_POST_PRODUCT", DEFAULT_PRODUCT)).strip(),
            platform=platform,
            lookback_days=lookback,
            lock_path=str(
                source.get("TT_AUTO_POST_METRIC_LOCK_PATH", DEFAULT_LOCK_PATH)
            ).strip(),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if (
            not self.db_path
            or self.mysql_host != DEFAULT_MYSQL_HOST
            or self.mysql_port != DEFAULT_MYSQL_PORT
            or not self.mysql_user
            or self.mysql_password == ""
            or self.mysql_database != DEFAULT_SCHEMA
            or self.blacklist_database != DEFAULT_BLACKLIST_SCHEMA
            or self.platform != DEFAULT_PLATFORM
            or self.lookback_days < 1
            or self.lookback_days > 30
            or self.product != DEFAULT_PRODUCT
            or self.lock_path != DEFAULT_LOCK_PATH
        ):
            raise ValueError("metric runner configuration is invalid")

    def __repr__(self) -> str:
        return (
            "MetricRunnerConfig(db_path=%r,mysql_host=%r,mysql_port=%r,"
            "mysql_user=%r,mysql_password=<redacted>,mysql_database=%r,"
            "platform=%r,lookback_days=%r)"
            % (
                self.db_path,
                self.mysql_host,
                self.mysql_port,
                self.mysql_user,
                self.mysql_database,
                self.platform,
                self.lookback_days,
            )
        )


def requested_metric_dates(
    *,
    explicit_dates: Sequence[str],
    lookback_days: int,
    now: Optional[datetime] = None,
) -> List[str]:
    complete = set(complete_beijing_dates(now, 30))
    if explicit_dates:
        normalized = list(dict.fromkeys(normalize_metric_date(value) for value in explicit_dates))
        invalid = [value for value in normalized if value not in complete]
        if invalid:
            raise ValueError(
                "metric refresh accepts only the previous 30 complete Beijing days"
            )
        return sorted(normalized)
    return list(complete_beijing_dates(now, int(lookback_days)))


def execute_metric_refresh(
    source: ReadOnlyMySQLRepository,
    store: Any,
    *,
    metric_dates: Iterable[str],
    platform: int = DEFAULT_PLATFORM,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    if isinstance(platform, bool) or int(platform) != DEFAULT_PLATFORM:
        raise ValueError("metric platform must be exactly 0")
    dates = list(dict.fromkeys(normalize_metric_date(value) for value in metric_dates))
    if not dates:
        raise ValueError("at least one metric date is required")
    completed = []
    for metric_date in dates:
        item = refresh_metric_day(
            source,
            store,
            metric_date,
            platform=int(platform),
            refreshed_at=now,
        )
        if not isinstance(item, Mapping):
            raise RuntimeError("metric activation returned invalid data")
        completed.append(
            {
                "metric_date": metric_date,
                "platform": int(platform),
                "generation_id": int(
                    item.get("generation_id") or item.get("id") or 0
                ),
                "status": str(item.get("status") or "ready"),
            }
        )
    return {
        "ok": True,
        "platform": int(platform),
        "product": source.product,
        "completed": completed,
    }


@contextlib.contextmanager
def metric_refresh_lock(lock_path: str):
    """Acquire the independent metric refresh lock without waiting."""

    path = Path(str(lock_path or ""))
    if not path.is_absolute() or str(path) != DEFAULT_LOCK_PATH:
        raise ValueError("metric lock path is invalid")
    try:
        import fcntl
    except ImportError:
        raise RuntimeError("metric refresh lock requires fcntl") from None
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh TT auto-post daily metric generations",
    )
    parser.add_argument(
        "--date",
        action="append",
        default=[],
        help="one complete Beijing date (repeatable); defaults to lookback",
    )
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--platform", type=int, default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = MetricRunnerConfig.from_env()
        lookback = (
            config.lookback_days
            if args.lookback_days is None
            else int(args.lookback_days)
        )
        platform = config.platform if args.platform is None else int(args.platform)
        if platform != DEFAULT_PLATFORM:
            raise ValueError("metric platform must be exactly 0")
        if lookback < 1 or lookback > 30:
            raise ValueError("metric lookback must be between 1 and 30 days")
        now = datetime.now(UTC)
        dates = requested_metric_dates(
            explicit_dates=args.date,
            lookback_days=lookback,
            now=now,
        )

        def connection_factory() -> Any:
            return connect_read_only(
                host=config.mysql_host,
                port=config.mysql_port,
                user=config.mysql_user,
                password=config.mysql_password,
                database=config.mysql_database,
                connect_timeout=5,
                read_timeout=120,
            )

        source = ReadOnlyMySQLRepository(
            connection_factory,
            schema=config.mysql_database,
            blacklist_schema=config.blacklist_database,
            product=config.product,
            now_fn=lambda: now,
        )
        # Imported only for real execution so offline selector/runner tests do
        # not depend on the service storage implementation being initialized.
        from features.tt_auto_posts.core import TTPostAutoStore

        store = TTPostAutoStore(config.db_path)
        with metric_refresh_lock(config.lock_path) as acquired:
            if not acquired:
                print(
                    json.dumps(
                        {"ok": True, "skipped": "metric_refresh_lock_busy"},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            result = execute_metric_refresh(
                source,
                store,
                metric_dates=dates,
                platform=platform,
                now=now,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        code = str(getattr(exc, "code", "tt_auto_metric_refresh_failed"))
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": str(exc)[:500],
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
