"""Freeze an ordinary same-bucket FB source without downloading the full video."""
import hashlib
import json
import math
import re
import subprocess
from urllib.parse import unquote, urlsplit

from .cloud_jobs import fingerprint
from .executors import ExecutionError


def cloud_input(executor, request, identity):
    jobs = executor.jobs
    ledger = jobs.ledger
    action = fingerprint([identity, "render"])
    with ledger.connection() as conn:
        saved = conn.execute("SELECT object_key,seconds FROM fb_input WHERE action=?", (action,)).fetchone()
        if saved:
            return tuple(saved)
        if conn.execute("SELECT 1 FROM action WHERE identity=?", (action,)).fetchone():
            return None  # Keep the request digest of an already-submitted legacy job.

    if (not executor.config.get("fb_cloud_input") or request.get("source_sha256") or
            request.get("source_trim_tail_seconds", 0) or
            request.get("video_template", "random_overlay") != "random_overlay"):
        return None
    url = request["source_url"]
    parsed = urlsplit(url)
    key = unquote(parsed.path[1:])
    if (parsed.scheme != "https" or parsed.netloc != jobs.bucket + ".cos." + jobs.region + ".myqcloud.com" or
            parsed.query or parsed.fragment or not key.startswith("custom/source/") or
            any(part in {".", ".."} for part in key.split("/"))):
        return None
    try:
        source = executor.origin.head(key)
        if not source:
            return None
        size = int(source["content-length"])
        etag = source["etag"]
        crc = source.get("x-cos-hash-crc64ecma")
        if not crc or not re.fullmatch(r'"[a-fA-F0-9-]+"', etag) or not 0 < size <= 2 * 1024**3:
            return None
        if request.get("source_size") and int(request["source_size"]) != size:
            raise ExecutionError("cos_source_fingerprint_changed")

        # FFprobe seeks over HTTP ranges. Pin every read and the subsequent copy
        # to the same ETag. A non-seekable input takes the ordinary local path.
        if source.get("accept-ranges", "").lower() != "bytes":
            return None
        args = [executor.config["ffprobe"], "-v", "error", "-protocol_whitelist", "https,tls,tcp",
                "-f", "mov", "-enable_drefs", "0", "-rw_timeout", "8000000", "-headers", "If-Match: " + etag + "\r\n",
                "-show_entries", "format=duration:stream=codec_type", "-of", "json", url]
        raw = json.loads(subprocess.run(args, capture_output=True, check=True, timeout=25).stdout)
        types = {s.get("codec_type") for s in raw["streams"]}
        seconds = float(raw["format"]["duration"])
        if not math.isfinite(seconds) or not 0 < seconds <= 3600 or not {"audio", "video"} <= types:
            return None
        version = hashlib.sha256((key + "\n" + etag).encode()).hexdigest()
        dest = jobs.prefix + "/input-snapshots/" + version + ".mp4"
        copied = executor.origin.head(dest)
        if copied is None:
            jobs.client.copy_object(Bucket=jobs.bucket, Key=dest, CopySourceIfMatch=etag,
                CopySource={"Bucket": jobs.bucket, "Region": jobs.region, "Key": key})
            copied = executor.origin.head(dest)
        if not copied or int(copied["content-length"]) != size or copied.get("x-cos-hash-crc64ecma") != crc:
            return None
    except ExecutionError:
        raise
    except Exception as exc:
        print("cos_fb_input_fallback", type(exc).__name__, flush=True)
        return None

    with ledger.connection() as conn:
        conn.execute("INSERT OR IGNORE INTO fb_input(action,object_key,seconds) VALUES(?,?,?)", (action, dest, seconds))
        return tuple(conn.execute("SELECT object_key,seconds FROM fb_input WHERE action=?", (action,)).fetchone())
