#!/usr/bin/env python3
"""Re-prepare available TikTok recurring materials under one exact profile.

The command is a dry run unless ``--apply`` is supplied. It never creates a
TikTok publish request. Each new GPU artifact is prepared before one fenced
SQLite transaction replaces the matching available pool row and its ready
intake ledger row.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.tt_posts.core import TTPostError, TTPostStore  # noqa: E402
from features.tt_posts.service import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_GPU_URL,
    GPUClient,
)


DEFAULT_LOCK_PATH = "/run/tt-post/profile-upgrade.lock"
GPU_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{8,128}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ProfileUpgradeError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = str(code or "tt_post_profile_upgrade_failed")[:96]
        super().__init__(str(message or "TT Post profile upgrade failed")[:500])


def preparation_job_id(
    material: Mapping[str, Any],
    target_profile: str,
    source_trim_tail_seconds: float,
) -> str:
    source_url_hash = hashlib.sha256(
        str(material["source_media_url"]).encode("utf-8")
    ).hexdigest()
    identity = "|".join(
        (
            str(material["material_id"]),
            str(material["content_id"]),
            source_url_hash,
            target_profile,
            str(source_trim_tail_seconds),
        )
    )
    return "ttpreview-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:36]


def _https_url(value: Any) -> str:
    normalized = str(value or "").strip()
    parsed = urllib.parse.urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProfileUpgradeError(
            "tt_post_profile_upgrade_artifact_invalid",
            "GPU prepared media URL is invalid",
        )
    return normalized


def validate_prepared_artifact(
    prepared: Mapping[str, Any],
    *,
    material: Mapping[str, Any],
    job_id: str,
    target_profile: str,
    source_trim_tail_seconds: float,
) -> Dict[str, Any]:
    if not isinstance(prepared, Mapping):
        raise ProfileUpgradeError(
            "tt_post_profile_upgrade_artifact_invalid",
            "GPU prepare response is invalid",
        )
    returned_job_id = str(prepared.get("job_id") or "").strip()
    returned_content_id = str(prepared.get("content_id") or "").strip()
    returned_profile = str(prepared.get("profile") or "").strip()
    if (
        returned_job_id != job_id
        or not GPU_JOB_ID_RE.fullmatch(returned_job_id)
        or returned_content_id != str(material["content_id"])
        or returned_profile != target_profile
    ):
        raise ProfileUpgradeError(
            "tt_post_profile_upgrade_artifact_mismatch",
            "GPU prepared media identity does not match the requested upgrade",
        )
    prepared_url = _https_url(
        prepared.get("output_url")
        or prepared.get("prepared_media_url")
        or prepared.get("final_media_url")
    )
    if prepared_url == str(material["source_media_url"]):
        raise ProfileUpgradeError(
            "tt_post_profile_upgrade_artifact_mismatch",
            "GPU prepared media URL matches the source URL",
        )
    output_sha256 = str(prepared.get("output_sha256") or "").strip().lower()
    try:
        output_size = int(prepared.get("output_size"))
        probe = prepared.get("probe") if isinstance(prepared.get("probe"), Mapping) else {}
        duration = float(probe.get("duration") or 0)
    except (TypeError, ValueError, OverflowError):
        output_size = 0
        duration = 0
    if (
        not SHA256_RE.fullmatch(output_sha256)
        or output_size <= 0
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise ProfileUpgradeError(
            "tt_post_profile_upgrade_artifact_invalid",
            "GPU prepared media metrics are invalid",
        )
    return {
        "prepared_media_url": prepared_url,
        "gpu_job_id": job_id,
        "prepared_output_sha256": output_sha256,
        "prepared_output_size": output_size,
        "prepared_duration_sec": duration,
        "source_trim_tail_seconds": source_trim_tail_seconds,
        "preparation_profile": target_profile,
    }


class ProfileUpgradeRunner:
    def __init__(
        self,
        store: TTPostStore,
        prepare: Optional[Callable[..., Mapping[str, Any]]],
        *,
        target_profile: str,
        source_trim_tail_seconds: float,
    ):
        self.store = store
        self.prepare = prepare
        self.target_profile = str(target_profile or "").strip()
        try:
            self.source_trim_tail_seconds = float(source_trim_tail_seconds)
        except (TypeError, ValueError, OverflowError):
            self.source_trim_tail_seconds = -1
        if (
            not self.target_profile
            or len(self.target_profile) > 128
            or not math.isfinite(self.source_trim_tail_seconds)
            or self.source_trim_tail_seconds < 0
        ):
            raise ProfileUpgradeError(
                "tt_post_profile_upgrade_config_invalid",
                "TT Post target profile configuration is invalid",
            )

    def run(
        self,
        source_profile: str,
        *,
        limit: int,
        apply: bool,
    ) -> Dict[str, Any]:
        candidates = self.store.list_available_recurring_profile_upgrades(
            source_profile,
            self.target_profile,
            limit=limit,
        )
        result = {
            "applied": bool(apply),
            "candidate_count": len(candidates),
            "candidate_pool_item_ids": [int(item["id"]) for item in candidates],
            "source_profile": source_profile,
            "target_profile": self.target_profile,
            "upgraded_count": 0,
            "upgraded_pool_item_ids": [],
        }
        if not apply:
            return result
        if self.prepare is None:
            raise ProfileUpgradeError(
                "tt_post_profile_upgrade_config_invalid",
                "GPU prepare client is required in apply mode",
            )
        for material in candidates:
            job_id = preparation_job_id(
                material,
                self.target_profile,
                self.source_trim_tail_seconds,
            )
            prepared = self.prepare(
                job_id=job_id,
                material={
                    "material_id": str(material["material_id"]),
                    "content_id": str(material["content_id"]),
                    "source_media_url": str(material["source_media_url"]),
                },
                source_trim_tail_seconds=self.source_trim_tail_seconds,
                expected_profile=self.target_profile,
            )
            artifact = validate_prepared_artifact(
                prepared,
                material=material,
                job_id=job_id,
                target_profile=self.target_profile,
                source_trim_tail_seconds=self.source_trim_tail_seconds,
            )
            upgraded = self.store.upgrade_available_recurring_artifact(
                material["id"],
                expected_preparation_profile=source_profile,
                expected_gpu_job_id=material["gpu_job_id"],
                expected_output_sha256=material["prepared_output_sha256"],
                **artifact,
            )
            result["upgraded_count"] += 1
            result["upgraded_pool_item_ids"].append(int(upgraded["id"]))
        return result


@contextlib.contextmanager
def process_lock(path: str) -> Iterator[bool]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            yield True
            return
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-profile", required=True)
    parser.add_argument(
        "--to-profile",
        default=os.environ.get("TT_POST_MEDIA_PROFILE_VERSION", ""),
    )
    parser.add_argument(
        "--source-trim-tail-seconds",
        type=float,
        default=float(
            os.environ.get("TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS", "0")
        ),
    )
    parser.add_argument("--db-path", default=os.environ.get("TT_POST_DB_PATH", DEFAULT_DB_PATH))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.limit < 1 or args.limit > 1000:
        raise ProfileUpgradeError(
            "tt_post_profile_upgrade_config_invalid",
            "TT Post profile upgrade limit is invalid",
        )
    store = TTPostStore(args.db_path)
    prepare = None
    if args.apply:
        gpu = GPUClient(
            os.environ.get("TT_POST_GPU_URL", DEFAULT_GPU_URL),
            os.environ.get("TT_POST_GPU_INTERNAL_TOKEN", ""),
            os.environ.get("TT_POST_GPU_CREDENTIAL_SEAL_KEY_B64", ""),
            timeout=int(os.environ.get("TT_POST_GPU_TIMEOUT", "300")),
            prepare_timeout=int(os.environ.get("TT_POST_GPU_PREPARE_TIMEOUT", "9000")),
        )
        prepare = gpu.prepare
    runner = ProfileUpgradeRunner(
        store,
        prepare,
        target_profile=args.to_profile,
        source_trim_tail_seconds=args.source_trim_tail_seconds,
    )
    with process_lock(args.lock_path) as acquired:
        if not acquired:
            print(json.dumps({"status": "locked"}, sort_keys=True))
            return 0
        result = runner.run(
            args.from_profile,
            limit=args.limit,
            apply=args.apply,
        )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ProfileUpgradeError, TTPostError) as exc:
        print(
            json.dumps(
                {
                    "code": getattr(exc, "code", "tt_post_profile_upgrade_failed"),
                    "status": "failed",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
