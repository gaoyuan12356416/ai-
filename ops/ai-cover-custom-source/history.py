"""Read-only reconstruction of legacy cover uploads for one explicit cutoff.

No publisher imports, remote writes, POSTs, or ledger mutations.  All timestamps
returned here are UTC; naive SQLite audit timestamps are UTC, while legacy
catalog created_at is deliberately never used as an acceptance timestamp.
"""
import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from features.cover_material_source import Catalog, SPECS, encoded, literal, readonly

UTC = dt.timezone.utc
SYNC_DB = "/mnt/data-disk/youtube-cover-material-sync/sync.sqlite3"
SOURCE_DB = "/root/drama_material_service/data/drama_material_jobs.sqlite3"
TRIAL_ROOT = "/mnt/data-disk/youtube-cover-material-trial"
LEGACY_PRODUCTS = {1479: "Drama-AI素材", 979: "freereels-AI素材",
                   3360: "moboreels", 2373: "MoboShort"}


def timestamp(value, *, naive_utc=False):
    value = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        if not naive_utc:
            raise ValueError("cutoff_requires_explicit_timezone")
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _issue(kind, key, reason, **detail):
    return dict(source_kind=kind, source_key=key, reason=reason, **detail)


def load_adoption_receipts(trial_root=TRIAL_ROOT):
    """Read the separately authorized one-image trials, without copying secrets."""
    receipts = {}
    for response_path in sorted(Path(trial_root).glob("20??????/*/response.json")):
        folder = response_path.parent
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            attempt = json.loads((folder / "attempt.json").read_text(encoding="utf-8"))
            confirmed = json.loads((folder / "confirmed.json").read_text(encoding="utf-8"))
            request = manifest["request"]
            request_sha = hashlib.sha256(json.dumps(
                request, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            at = timestamp(response["received_at_utc"])
            if (response["http_status"] != 200 or response["body"].get("code") != 0
                    or len(request) != 1 or attempt["state"] != "outbound_started"
                    or attempt["request_sha256"] != request_sha
                    or timestamp(attempt["at_utc"]) > at
                    or confirmed["state"] != "confirmed" or confirmed["formal_count"] != 1
                    or confirmed["image"].get("matches_approved_sha256") is not True
                    or confirmed["image"]["sha256"] != manifest["asset_sha256"]):
                continue
            key = "youtube-cover:{task_id}:v{version}:{asset_id}".format(**manifest)
            if key in receipts:
                raise ValueError("duplicate_adoption_receipts")
            receipts[key] = dict(
                accepted_at=at.isoformat(), intake_id=int(confirmed["intake_id"]),
                final_id=int(confirmed["final_material"]["id"]),
                material_id=str(request[0]["material_id"]),
                source_sha256=manifest["asset_sha256"],
                request_sha256=request_sha, request=request[0],
                evidence_ref=str(response_path),
                evidence_sha256=hashlib.sha256(response_path.read_bytes()).hexdigest())
        except (KeyError, TypeError, OSError, json.JSONDecodeError):
            # Unfinished trials cannot supply acceptance evidence.
            continue
    return receipts


def _local_image(asset, accepted, category, expected_sha=""):
    """Hash retained images and reject evidence of post-acceptance replacement."""
    from PIL import Image
    paths = []
    for field in ("public_output_path", "workspace_output_path", "path"):
        path = Path(asset.get(field) or "")
        if path.is_file() and path not in paths:
            paths.append(path)
    if not paths:
        raise ValueError("retained_image_missing")
    results = []
    for path in paths:
        stat = path.stat()
        if stat.st_size <= 0 or stat.st_size > 30 * 1024 * 1024:
            raise ValueError("retained_image_size_invalid")
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        # A durable SHA is stronger than a file timestamp (copied files can have
        # newer mtimes). Legacy screenshot assets usually have no saved SHA.
        if expected_sha:
            if sha != expected_sha:
                raise ValueError("retained_image_sha_changed")
        elif stat.st_mtime > accepted.timestamp() + 2:
            raise ValueError("image_modified_after_acceptance")
        with Image.open(path) as image:
            width, height = image.size
            image.verify()
        left, right = map(float, category.split(":"))
        if abs(width / height / (left / right) - 1) > 0.025:
            raise ValueError("retained_image_ratio_mismatch")
        results.append(dict(path=str(path), sha256=sha, width=width,
                            height=height, byte_length=len(raw),
                            retained_mtime=dt.datetime.fromtimestamp(
                                stat.st_mtime, UTC).isoformat()))
    if len({x["sha256"] for x in results}) != 1:
        raise ValueError("retained_copies_disagree")
    return results[0]


def _catalog_rows(catalog, where, limit):
    # Catalog.rows intentionally omits remark; legacy intake->final is stored
    # in remark, so request this explicit, bounded read-only projection.
    fields = ("id", "product", "type", "data_source", "data_source_id",
              "name", "url", "category", "remark", "user_id", "initiator",
              "is_delete", "created_at")
    projection = ",".join("'" + name + "',`" + name + "`" for name in fields)
    return catalog.query("SELECT JSON_OBJECT(" + projection
                         + ") FROM ads_custom_source WHERE " + where
                         + " LIMIT " + str(int(limit)))


def _legacy_readback(catalog, candidates):
    mids = sorted({str(x["legacy_material_id"]) for x in candidates})
    intakes = []
    for offset in range(0, len(mids), 50):
        batch = mids[offset:offset + 50]
        intakes.extend(_catalog_rows(
            catalog, "data_source=102 AND data_source_id IN ("
            + ",".join(literal(x) for x in batch) + ")", len(batch) * 2 + 1))
    final_ids = sorted({int(x["remark"]) for x in intakes
                        if re.fullmatch(r"[1-9][0-9]*", str(x.get("remark") or ""))})
    finals = {}
    for offset in range(0, len(final_ids), 50):
        batch = final_ids[offset:offset + 50]
        for row in _catalog_rows(catalog, "id IN (" + ",".join(map(str, batch)) + ")", len(batch) + 1):
            finals[int(row["id"])] = row
    by_mid = {}
    for row in intakes:
        by_mid.setdefault(str(row["data_source_id"]), []).append(row)
    return by_mid, finals


def _screenshot_candidates(conn, start, end):
    rows = conn.execute("""
        SELECT id, target_id, detail_json, created_at
        FROM drama_admin_audit_log
        WHERE action IN ('callback_screenshot_ai_source',
                         'callback_screenshot_ai_source_replay')
          AND created_at >= ? AND created_at <= ?
        ORDER BY id
    """, (start.strftime("%Y-%m-%d %H:%M:%S"),
          end.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    accepted, issues = {}, []
    for row in rows:
        if not start <= timestamp(row["created_at"], naive_utc=True) < end:
            continue
        key = "screenshot:" + row["target_id"]
        try:
            detail = json.loads(row["detail_json"])
            receipt = json.loads(detail.get("response", ""))
        except (ValueError, TypeError):
            issues.append(_issue("screenshot", key, "invalid_legacy_audit",
                                 audit_id=row["id"]))
            continue
        if (not isinstance(receipt, dict) or str(receipt.get("code")) != "0"
                or not 200 <= int(detail.get("status_code") or 0) < 300
                or int(detail.get("sent_count") or 0) != 3):
            issues.append(_issue("screenshot", key, "legacy_callback_not_accepted",
                                 audit_id=row["id"], excluded=True))
            continue
        accepted.setdefault(row["target_id"], []).append(dict(row))
    output = []
    for job_id, audits in accepted.items():
        row = conn.execute("SELECT * FROM drama_screenshot_job WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            issues.append(_issue("screenshot", "screenshot:" + job_id,
                                 "accepted_job_missing", audit_ids=[x["id"] for x in audits]))
            continue
        job = dict(row)
        latest = timestamp(audits[-1]["created_at"], naive_utc=True)
        earliest = timestamp(audits[0]["created_at"], naive_utc=True)
        try:
            assets = json.loads(job["assets_json"])
            system = (str(job.get("creator_user_id") or "") in (
                "screenshot-api-direct", "api:screenshot-api-direct")
                or job.get("creator_name") == "screenshot-api-direct")
            emails = [] if system else [r[0] for r in conn.execute(
                "SELECT DISTINCT LOWER(TRIM(email)) FROM drama_admin_user WHERE user_id=? LIMIT 3",
                (job["creator_user_id"],)).fetchall()]
            if not system and (len(emails) != 1 or not emails[0]):
                raise ValueError("creator_email_not_unique")
        except (ValueError, TypeError) as error:
            issues.append(_issue("screenshot", "screenshot:" + job_id, str(error)))
            continue
        for size, category in SPECS:
            key = "screenshot:" + job_id + ":" + size
            try:
                asset = assets.get(size) or {}
                url = str(job.get(size + "_url") or "")
                if not url or asset.get("url") != url:
                    raise ValueError("asset_url_identity_mismatch")
                image = _local_image(asset, latest, category, asset.get("sha256") or "")
                # Without old per-version SHA, two accepted generations cannot
                # be collapsed to the current bytes.
                if (len(audits) > 1 and timestamp(image["retained_mtime"]) > earliest + dt.timedelta(seconds=2)
                        and not asset.get("sha256")):
                    raise ValueError("multiple_accepted_versions_unrecoverable")
                output.append(dict(
                    source_kind="screenshot", source_key=key,
                    app_id=int(job["app_id"]), content_id=str(job["content_id"]),
                    category=category,
                    name="{}-{}-{}-{}".format(job["content_id"], job.get("drama_name") or "screenshot",
                                             category, job_id[:8]),
                    url=url, language=job["language"], country=job["country"],
                    owner_name=job.get("creator_name") or "",
                    owner_email="" if system else emails[0], system_actor=system,
                    generated_at=job.get("finished_at") or "",
                    is_backfill=True, backfill_kind="migration", legacy_final_ids=[],
                    legacy_material_id=str(int(hashlib.sha1((job_id + ":" + size).encode()).hexdigest()[:15], 16)),
                    legacy_accepted_at=latest.isoformat(),
                    legacy_evidence=dict(kind="screenshot_callback_receipt",
                                         audit_ids=[x["id"] for x in audits],
                                         accepted_at_utc=[timestamp(x["created_at"], naive_utc=True).isoformat() for x in audits],
                                         receipt_sha256=[hashlib.sha256(x["detail_json"].encode()).hexdigest() for x in audits]),
                    **image))
            except (ValueError, TypeError, KeyError, OSError) as error:
                issues.append(_issue("screenshot", key, str(error),
                                     audit_ids=[x["id"] for x in audits],
                                     legacy_accepted_at=latest.isoformat()))
    return output, issues


def _youtube_candidates(conn, start, end, adoption_receipts):
    accepted = {}
    outbound = set()
    for row in conn.execute("SELECT * FROM events ORDER BY id"):
        if row["state"] == "outbound_started":
            outbound.add(row["key"])
        try:
            detail = json.loads(row["detail"])
            if detail.get("http_status") is not None:
                outbound.add(row["key"])
            if (row["state"] == "accepted" and detail.get("accepted") is True
                    and detail.get("http_status") == 200 and detail.get("code") == 0):
                accepted.setdefault(row["key"], []).append(dict(row))
        except (ValueError, TypeError):
            continue
    output, issues = [], []
    for row in conn.execute("SELECT * FROM cover_sync ORDER BY key"):
        row = dict(row)
        key = row["key"]
        audits = accepted.get(key, [])
        evidence = None
        if audits:
            first = audits[0]
            at = timestamp(first["created_at"])
            evidence = dict(kind="youtube_callback_receipt",
                            event_ids=[x["id"] for x in audits],
                            receipt_sha256=hashlib.sha256(first["detail"].encode()).hexdigest())
        elif key in adoption_receipts:
            evidence = dict(adoption_receipts[key])
            if (int(evidence.get("intake_id") or 0) != int(row.get("intake_id") or 0)
                    or int(evidence.get("final_id") or 0) != int(row.get("final_id") or 0)
                    or not evidence.get("evidence_ref") or not evidence.get("evidence_sha256")):
                issues.append(_issue("youtube", key, "invalid_adoption_evidence"))
                continue
            at = timestamp(evidence["accepted_at"])
            evidence["kind"] = "youtube_adopted_durable_receipt"
        else:
            if row["state"] == "pending" and int(row.get("attempts") or 0) == 0 and key not in outbound:
                issues.append(_issue("youtube", key, "legacy_pending_never_uploaded",
                                     excluded=True, attempts=0, current_state="pending"))
                continue
            issues.append(_issue("youtube", key, "accepted_time_evidence_missing",
                                 intake_id=row.get("intake_id"), final_id=row.get("final_id"),
                                 current_state=row["state"]))
            continue
        if not start <= at < end:
            continue
        try:
            source = json.loads(row["source"])
            frozen = json.loads(row["request"])
            request = frozen["payload"]
            if (source["content_id"] != request["content_id"]
                    or source["category"] != request["category"]
                    or str(request["material_id"]) != row["material_id"]):
                raise ValueError("youtube_request_identity_mismatch")
            if evidence["kind"] == "youtube_adopted_durable_receipt":
                if (evidence.get("material_id") and evidence["material_id"] != row["material_id"]
                        or evidence.get("source_sha256") and evidence["source_sha256"] != source["sha256"]
                        or evidence.get("request") and evidence["request"] != request):
                    raise ValueError("adoption_receipt_source_mismatch")
            image = _local_image(source, at, source["category"], source["sha256"])
            output.append(dict(
                source_kind="youtube", source_key=key, app_id=1479,
                content_id=str(source["content_id"]), category=source["category"],
                name=request["name"], url=request["url"], language=source["language"],
                country=source.get("country") or "", owner_email=source["owner_email"],
                owner_name=source["owner_name"], system_actor=False,
                generated_at=source["asset_created_at"], cover_source=source.get("cover_source"),
                version=source["version"], asset_id=source["asset_id"],
                is_backfill=True, backfill_kind="migration", legacy_final_ids=[],
                legacy_material_id=row["material_id"], legacy_accepted_at=at.isoformat(),
                legacy_evidence=evidence, **image))
        except (ValueError, TypeError, KeyError, OSError) as error:
            issues.append(_issue("youtube", key, str(error), legacy_accepted_at=at.isoformat()))
    return output, issues


def discover(cutoff, source_db, sync_db, catalog, adoption_receipts=None):
    """Return (verified_sources, issues) for [cutoff - 168h, cutoff).

    Caller freezes returned objects and issues atomically. Do not regenerate a
    cutoff for resume. Optional adoption receipts must be independently audited;
    generated-at, catalog created_at, and empty accepted events are insufficient.
    Catalog exceptions propagate, because read failure must never mean zero.
    """
    end = timestamp(cutoff)
    start = end - dt.timedelta(hours=168)
    with contextlib.closing(readonly(source_db)) as conn:
        conn.execute("BEGIN")
        screenshots, issues = _screenshot_candidates(conn, start, end)
    with contextlib.closing(readonly(sync_db)) as conn:
        conn.execute("BEGIN")
        youtube, yt_issues = _youtube_candidates(
            conn, start, end,
            load_adoption_receipts() if adoption_receipts is None else adoption_receipts)
    issues.extend(yt_issues)
    candidates = screenshots + youtube
    output, readback_issues = verify_legacy_candidates(candidates, catalog)
    issues.extend(readback_issues)
    return output, issues


def verify_legacy_candidates(candidates, catalog, *, recheck_images=False):
    """Reconcile retained candidates without changing source identity or cutoff."""
    by_mid, finals = _legacy_readback(catalog, candidates)
    output = []
    issues = []
    for original in candidates:
        source = copy.deepcopy(original)
        matches = by_mid.get(str(source["legacy_material_id"]), [])
        base = dict(source_kind=source["source_kind"], source_key=source["source_key"],
                    legacy_accepted_at=source["legacy_accepted_at"])
        if recheck_images:
            try:
                _local_image(dict(path=source["path"]),
                             timestamp(source["legacy_accepted_at"]),
                             source["category"], source["sha256"])
            except (KeyError, ValueError, TypeError, OSError) as error:
                issues.append(dict(base, reason=str(error), candidate=source))
                continue
        if len(matches) != 1:
            issues.append(dict(base, reason="legacy_intake_not_unique", count=len(matches)))
            continue
        intake = matches[0]
        mismatch = [k for k in ("url", "name", "category") if str(intake.get(k)) != str(source[k])]
        if mismatch:
            issues.append(dict(base, reason="legacy_intake_identity_mismatch", fields=mismatch))
            continue
        final_id = int(intake["remark"]) if re.fullmatch(r"[1-9][0-9]*", str(intake.get("remark") or "")) else 0
        final = finals.get(final_id)
        if not final:
            issues.append(dict(base, reason="legacy_final_pending", intake_id=intake["id"],
                               candidate=source))
            continue
        expected = dict(product=LEGACY_PRODUCTS.get(source["app_id"], "__unsupported__"),
                        type=1, data_source=6, data_source_id=source["content_id"],
                        name=source["name"], category=source["category"], is_delete=0)
        mismatch = [k for k, v in expected.items() if str(final.get(k)) != str(v)]
        if mismatch:
            issues.append(dict(base, reason="legacy_final_mismatch", final_id=final_id, fields=mismatch))
            continue
        source["legacy_intake_id"] = int(intake["id"])
        source["legacy_final_ids"] = [final_id]
        source["legacy_evidence"].update(
            intake_created_at_not_acceptance=intake["created_at"],
            final_created_at_not_acceptance=final["created_at"])
        output.append(source)
    return sorted(output, key=lambda x: x["source_key"]), issues


def main(argv=None):
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", required=True, help="Explicit ISO8601 cutoff with timezone")
    parser.add_argument("--source-db", default=SOURCE_DB)
    parser.add_argument("--sync-db", default=SYNC_DB)
    parser.add_argument("--adoption-receipts", help="Audited key -> receipt evidence JSON")
    args = parser.parse_args(argv)
    receipts = json.loads(Path(args.adoption_receipts).read_text()) if args.adoption_receipts else None
    sources, issues = discover(args.cutoff, args.source_db, args.sync_db, Catalog(dict(os.environ)), receipts)
    print(encoded(dict(cutoff=timestamp(args.cutoff).isoformat(), count=len(sources),
                       sources=sources, issues=issues)))


if __name__ == "__main__":
    main()
