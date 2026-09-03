#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safely rotate the shared TT Business API token after compatibility canaries."""

from __future__ import print_function

import argparse
import collections
import getpass
import hashlib
import json
import os
import sqlite3
import stat
import sys
from datetime import timedelta

import tt_minis_bid_protection_sync as sync


REQUIRED_PRODUCT_IDS = (3346, 3380, 3416)
DEFAULT_BACKUP_DIR = "/mnt/data-disk/tt-minis-bid-protection/backups/token"
INTEGRATED_METRICS = [
    "spend",
    "native_growth_ad_revenue_value_d0",
    "native_growth_ad_revenue_roas_d0",
    "native_growth_total_ad_impression_value",
]


class RotationError(RuntimeError):
    pass


def token_hash(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def safe_error(exc, secrets=()):
    text = str(exc or "")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "<redacted>")
    return " ".join(text.split())[:500]


def load_token_row(path, token_key):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(tt_business_api_tokens)")]
        expected = {
            "token_key",
            "product_id",
            "access_token",
            "token_hash",
            "status",
            "purpose",
            "note",
            "created_at",
            "updated_at",
        }
        if set(columns) != expected:
            raise RotationError("unexpected tt_business_api_tokens schema")
        row = conn.execute(
            "SELECT token_key, product_id, access_token, token_hash, status, purpose, note, created_at, updated_at "
            "FROM tt_business_api_tokens WHERE token_key = ? LIMIT 1",
            (token_key,),
        ).fetchone()
    finally:
        conn.close()
    if not row or int(row[4]) != 1:
        raise RotationError("active token row was not found")
    names = [
        "token_key",
        "product_id",
        "access_token",
        "token_hash",
        "status",
        "purpose",
        "note",
        "created_at",
        "updated_at",
    ]
    result = dict(zip(names, row))
    if token_hash(result["access_token"]) != result["token_hash"]:
        raise RotationError("stored token hash does not match stored token")
    return result


def verify_production_paths(token_db, backup_dir):
    token_real = os.path.realpath(token_db)
    if token_real != "/root/codex_test/tt_business_api_tokens.sqlite3":
        raise RotationError("unexpected production token database path")
    mode = stat.S_IMODE(os.stat(token_real).st_mode)
    if mode != 0o600:
        raise RotationError("production token database mode must be 0600")
    if not os.path.ismount("/mnt/data-disk"):
        raise RotationError("/mnt/data-disk is not mounted")
    backup_real = os.path.realpath(backup_dir)
    allowed_root = "/mnt/data-disk/tt-minis-bid-protection/backups"
    if not (backup_real == allowed_root or backup_real.startswith(allowed_root + os.sep)):
        raise RotationError("backup directory is outside the approved data-disk root")


def backup_database(source_path, backup_dir):
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = sync.datetime.now(sync.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = os.path.join(backup_dir, "tt_business_api_tokens.sqlite3.%s.before_bid_protection" % stamp)
    source = sqlite3.connect("file:%s?mode=ro" % source_path, uri=True, timeout=10)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise RotationError("token backup integrity check failed")
    finally:
        target.close()
        source.close()
    os.chmod(backup_path, 0o600)
    return backup_path


def fetch_integrated_canary(client, advertiser_id, day):
    params = {
        "advertiser_id": sync.normalize_id(advertiser_id, "advertiser_id"),
        "report_type": "BASIC",
        "service_type": "AUCTION",
        "data_level": "AUCTION_ADGROUP",
        "dimensions": json.dumps(["adgroup_id", "stat_time_day"], separators=(",", ":")),
        "metrics": json.dumps(INTEGRATED_METRICS, separators=(",", ":")),
        "start_date": day,
        "end_date": day,
        "page": 1,
        "page_size": 1,
    }
    return client.get_json(sync.INTEGRATED_REPORT_URL, params)


def candidate_pool(day):
    rows = sync.build_day_candidates(day, "CAMPAIGN")
    by_product = {product_id: [] for product_id in REQUIRED_PRODUCT_IDS}
    seen = set()
    for row in rows:
        product_id = int(row["product_id"])
        if product_id not in by_product:
            continue
        key = (product_id, row["advertiser_id"])
        if key in seen:
            continue
        seen.add(key)
        by_product[product_id].append(row)
    missing = [str(product_id) for product_id, items in by_product.items() if not items]
    if missing:
        raise RotationError("no canary candidates for product_ids=%s" % ",".join(missing))
    return by_product


def capture_native_growth_baseline(access_token, day, pools):
    client = sync.TikTokBidProtectionClient(access_token, timeout=45, max_retries=2)
    baseline = {product_id: [] for product_id in REQUIRED_PRODUCT_IDS}
    failures = {}
    for product_id in REQUIRED_PRODUCT_IDS:
        product_failures = []
        for candidate in pools[product_id]:
            try:
                fetch_integrated_canary(client, candidate["advertiser_id"], day)
                baseline[product_id].append(candidate)
            except Exception as exc:
                product_failures.append(safe_error(exc, (access_token,)))
        if not baseline[product_id]:
            failures[product_id] = product_failures[-3:]
    if failures:
        raise RotationError(
            "current Native Growth token has no successful baseline for product_ids=%s"
            % ",".join(str(value) for value in sorted(failures))
        )
    return baseline


def run_compatibility_canaries(access_token, day, required_candidates):
    """Require the new token to preserve every currently working advertiser."""
    client = sync.TikTokBidProtectionClient(access_token, timeout=45, max_retries=2)
    failures = collections.defaultdict(list)
    successes = collections.Counter()
    for product_id in REQUIRED_PRODUCT_IDS:
        for candidate in required_candidates[product_id]:
            try:
                client.fetch_status(candidate["advertiser_id"], "CAMPAIGN", [candidate["query_id"]])
                client.fetch_history(
                    candidate["advertiser_id"], "CAMPAIGN", [candidate["query_id"]], day
                )
                fetch_integrated_canary(client, candidate["advertiser_id"], day)
                successes[product_id] += 1
            except Exception as exc:
                failures[product_id].append(safe_error(exc, (access_token,)))
    if failures:
        raise RotationError(
            "new token compatibility coverage regressed for product_ids=%s failed_accounts=%s"
            % (
                ",".join(str(value) for value in sorted(failures)),
                sum(len(values) for values in failures.values()),
            )
        )
    return dict(successes)


def update_token_row(path, token_key, old_row, new_token, note):
    new_hash = token_hash(new_token)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE tt_business_api_tokens "
            "SET access_token = ?, token_hash = ?, note = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE token_key = ? AND product_id IS ? AND access_token = ? AND token_hash = ? "
            "AND status = ? AND purpose IS ? AND note IS ? AND created_at IS ? AND updated_at IS ?",
            (
                new_token,
                new_hash,
                note,
                token_key,
                old_row["product_id"],
                old_row["access_token"],
                old_row["token_hash"],
                old_row["status"],
                old_row["purpose"],
                old_row["note"],
                old_row["created_at"],
                old_row["updated_at"],
            ),
        )
        if cursor.rowcount != 1:
            conn.execute("ROLLBACK")
            raise RotationError("token compare-and-swap updated an unexpected row count")
        row = conn.execute(
            "SELECT token_key, product_id, access_token, token_hash, status, purpose, note, created_at, updated_at "
            "FROM tt_business_api_tokens WHERE token_key = ? LIMIT 1",
            (token_key,),
        ).fetchone()
        conn.execute("COMMIT")
    finally:
        conn.close()
    names = [
        "token_key",
        "product_id",
        "access_token",
        "token_hash",
        "status",
        "purpose",
        "note",
        "created_at",
        "updated_at",
    ]
    return dict(zip(names, row))


def restore_token_row(path, token_key, old_row, expected_row):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE tt_business_api_tokens "
            "SET access_token = ?, token_hash = ?, note = ?, updated_at = ? "
            "WHERE token_key = ? AND product_id IS ? AND access_token = ? AND token_hash = ? "
            "AND status = ? AND purpose IS ? AND note IS ? AND created_at IS ? AND updated_at IS ?",
            (
                old_row["access_token"],
                old_row["token_hash"],
                old_row["note"],
                old_row["updated_at"],
                token_key,
                expected_row["product_id"],
                expected_row["access_token"],
                expected_row["token_hash"],
                expected_row["status"],
                expected_row["purpose"],
                expected_row["note"],
                expected_row["created_at"],
                expected_row["updated_at"],
            ),
        )
        if cursor.rowcount != 1:
            conn.execute("ROLLBACK")
            raise RotationError("token rollback compare-and-swap failed")
        conn.execute("COMMIT")
    finally:
        conn.close()


def rotate(token_db, token_key, backup_dir, new_token, day, require_production_paths=True):
    if not new_token or any(char.isspace() for char in new_token):
        raise RotationError("new token is empty or contains whitespace")
    if require_production_paths:
        verify_production_paths(token_db, backup_dir)
    old_row = load_token_row(token_db, token_key)
    if token_hash(new_token) == old_row["token_hash"]:
        raise RotationError("new token is identical to the stored token")

    pools = candidate_pool(day)
    baseline = capture_native_growth_baseline(old_row["access_token"], day, pools)
    baseline_counts = {str(key): len(values) for key, values in baseline.items()}
    sync.emit("token_canary_baseline", product_account_counts=baseline_counts)
    pre_counts = run_compatibility_canaries(new_token, day, baseline)
    sync.emit("token_canary_complete", phase="before_update", product_account_counts=pre_counts)
    backup_path = backup_database(token_db, backup_dir)
    previous_note = str(old_row.get("note") or "").strip()
    change_note = "rotated for Native Growth and bid protection after three-product canary"
    note = (previous_note + " | " + change_note).strip(" |")
    expected_row = update_token_row(token_db, token_key, old_row, new_token, note)
    try:
        persisted = load_token_row(token_db, token_key)
        if persisted["token_hash"] != token_hash(new_token):
            raise RotationError("persisted token hash verification failed")
        post_counts = run_compatibility_canaries(persisted["access_token"], day, baseline)
        sync.emit("token_canary_complete", phase="after_update", product_account_counts=post_counts)
    except Exception:
        restore_token_row(token_db, token_key, old_row, expected_row)
        raise
    return backup_path


def build_parser():
    parser = argparse.ArgumentParser(description="Rotate the shared TT Business API token safely")
    parser.add_argument("--token-db", default=sync.TOKEN_DB)
    parser.add_argument("--token-key", default=sync.TOKEN_KEY)
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--canary-date", help="completed date; defaults to Beijing yesterday")
    parser.add_argument("--token-stdin", action="store_true", help="read the new token from stdin without echo")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    day = args.canary_date or sync.format_day(sync.beijing_today() - timedelta(days=1))
    new_token = sys.stdin.readline().strip() if args.token_stdin else getpass.getpass("New TT token: ").strip()
    try:
        backup_path = rotate(args.token_db, args.token_key, args.backup_dir, new_token, day)
        sync.emit("token_rotation_complete", token_key=args.token_key, backup_path=backup_path)
        return 0
    except Exception as exc:
        sync.emit("token_rotation_failed", error=safe_error(exc, (new_token,)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
