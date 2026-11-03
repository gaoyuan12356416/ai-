"""Preview/send entrypoint, isolated from publishers and the existing daily ledger."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import shlex
import uuid

from features.post_daily_report.delivery import send_card, DefiniteFailure
from features.socialkit_analytics.refresh import ensure_data_mount
from features.socialkit_analytics.storage import atomic_json
from .collector import collect, safe_error, ComparisonUnavailable
from .cards import split_cards
from .delivery import Store, deliver

CHAT_ID = "oc_7c683c5770aef2e6c84a456e52cad389"
DEFAULT_ROOT = "/mnt/data-disk/socialkit-gap-alert"
DEFAULT_SOCIALKIT_ROOT = "/mnt/data-disk/socialkit-analytics"
DEFAULT_FEISHU_CONFIG = "/root/.codex/plugins/feishu/config.json"


def settings(env_files=()):
    # A systemd EnvironmentFile is already correctly decoded in os.environ.
    # CLI files are an optional dotenv fallback, never an override of that env.
    result = dict(os.environ)
    for filename in env_files:
        for line in Path(filename).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name in result:
                continue
            if value.strip().startswith(("'", '"')):
                pieces = shlex.split(value, comments=True)
                if len(pieces) != 1:
                    raise ValueError("invalid_env_file")
                value = pieces[0]
            result[name] = value.strip()
    result.setdefault("JOB_DB_PATH", "/root/drama_material_service/data/drama_material_jobs.sqlite3")
    result.setdefault("SOCIAL_ANALYTICS_X_DB_PATH", "/var/lib/x-post-automation/accounts.sqlite3")
    result.setdefault("SOCIAL_ANALYTICS_FACEBOOK_ATTRIBUTION_DB", "/mnt/data-disk/social-analytics/attribution.sqlite3")
    # No credentials are written to the report or returned in the summary.
    return result


@contextmanager
def run_lock(root):
    with (Path(root) / "run.lock").open("a+b") as handle:
        if os.name == "posix":
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        yield


def run_alerts(*, socialkit_root=DEFAULT_SOCIALKIT_ROOT, root=DEFAULT_ROOT,
               version=None, report_date=None, send=False, env_files=(),
               feishu_config=DEFAULT_FEISHU_CONFIG, now=None, collector=collect,
               sender=None, clock=None):
    # `now` selects the observation instant; it must never freeze the actual
    # transport date. Tests that simulate wall time inject `clock` explicitly.
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = now or clock()
    now = now.astimezone(timezone.utc)
    day = date.fromisoformat(report_date or now.date().isoformat()).isoformat()
    if send and day != clock().astimezone(timezone.utc).date().isoformat():
        raise ValueError("historical_sending_not_allowed")
    root = Path(root).resolve()
    ensure_data_mount(root)
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True)
    with run_lock(root):
        run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        folder = root / ("runs" if send else "previews") / run_id
        folder.mkdir(parents=True, exist_ok=False)
        try:
            report = collector(settings(env_files), socialkit_root, version, day, now,
                               enforce_fresh=send or day == now.date().isoformat())
            atomic_json(folder / "comparison.json", report)
            cards = [card for _, card in split_cards(report, report["events"], CHAT_ID)]
            atomic_json(folder / "cards.json", cards)
            outcome = {}
            if send:
                if clock().astimezone(timezone.utc).date().isoformat() != day:
                    raise ComparisonUnavailable("statistics_day_changed")
                transport = sender or (lambda card, request_uuid: send_card(feishu_config, CHAT_ID, card, request_uuid))
                def guarded_sender(card, request_uuid):
                    if clock().astimezone(timezone.utc).date().isoformat() != day:
                        raise DefiniteFailure("utc_day_changed_before_transport")
                    return transport(card, request_uuid)
                store = Store(root / "delivery.sqlite3")
                try:
                    outcome = deliver(store, report, CHAT_ID, guarded_sender)
                finally:
                    store.close()
                atomic_json(folder / "receipts.json", outcome)
            incomplete = bool(report["pending"] or report["errors"] or outcome.get("held_cards") or outcome.get("failed_cards"))
            summary = dict(status="partial" if incomplete else "complete", mode="send" if send else "preview",
                           date=day, version=report["socialkit"]["version"], run_id=run_id,
                           candidate_events=len(report["events"]), pending=len(report["pending"]),
                           errors=report["errors"], path=str(folder), finished_at=datetime.now(timezone.utc).isoformat(),
                           **outcome)
            atomic_json(folder / "summary.json", summary)
            atomic_json(root / ("status.json" if send else "preview-status.json"), summary)
            return summary
        except Exception as exc:
            failure = dict(status="failed", mode="send" if send else "preview", date=day,
                           run_id=run_id, error_code=safe_error(exc),
                           finished_at=datetime.now(timezone.utc).isoformat())
            atomic_json(folder / "summary.json", failure)
            atomic_json(root / ("status.json" if send else "preview-status.json"), failure)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preview or send newly detected SocialKit revenue gaps")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--send", action="store_true")
    parser.add_argument("--version")
    parser.add_argument("--date", dest="report_date")
    parser.add_argument("--socialkit-root", default=DEFAULT_SOCIALKIT_ROOT)
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--env-file", action="append", default=[])
    parser.add_argument("--feishu-config", default=DEFAULT_FEISHU_CONFIG)
    args = parser.parse_args(argv)
    try:
        result = run_alerts(socialkit_root=args.socialkit_root, root=args.root, version=args.version,
                            report_date=args.report_date, send=args.send, env_files=args.env_file,
                            feishu_config=args.feishu_config)
    except Exception as exc:
        print(json.dumps(dict(status="failed", error_code=safe_error(exc))))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 2
