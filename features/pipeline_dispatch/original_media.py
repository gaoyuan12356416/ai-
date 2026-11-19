"""Copy verified original bytes to TikTok's verified origin without a renderer."""

import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

SOCKET = "/run/pipeline-tt-original.sock"
MAX_BYTES = 4 * 1024**3


def copy_to_verified_origin(path, sha, size, origin):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(300)
        sock.connect(os.environ.get("PIPELINE_TT_ORIGINAL_SOCKET", SOCKET))
        with sock.makefile("rwb") as stream, Path(path).open("rb") as src:
            stream.write((json.dumps({"sha256": sha, "size": size}) + "\n").encode())
            shutil.copyfileobj(src, stream, length=1024**2)
            stream.flush()
            sock.shutdown(socket.SHUT_WR)
            reply = json.loads(stream.readline(4096))
    url = str(reply.get("url", ""))
    if (
        not url.startswith(origin.rstrip("/") + "/")
        or urlsplit(url).netloc != urlsplit(origin).netloc
    ):
        raise OSError("tt_original_storage_unavailable")
    return url


def store_stream(stream, directory, store):
    line = stream.readline(256)
    header = json.loads(line)
    sha, size = header["sha256"], header["size"]
    if (
        not re.fullmatch("[a-f0-9]{64}", sha)
        or type(size) is not int
        or not 0 < size <= MAX_BYTES
    ):
        raise ValueError("invalid_original_identity")
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(dir=directory) as spool:
        path = Path(spool) / "source.mp4"
        with path.open("wb") as output:
            left = size
            while left:
                chunk = stream.read(min(left, 1024**2))
                if not chunk:
                    raise ValueError("incomplete_original")
                output.write(chunk)
                digest.update(chunk)
                left -= len(chunk)
        if stream.read(1) or digest.hexdigest() != sha:
            raise ValueError("original_identity_changed")
        key = store.config.cos_prefix + "/original/" + sha + ".mp4"
        store.upload(key, path, sha, size)
        return store.url(key)


def main():
    import sys
    from types import SimpleNamespace
    from features.tt_gpu.worker import CosObjectStore

    def setting(name):
        return os.environ["TT_POST_GPU_COS_" + name]

    store = CosObjectStore(
        SimpleNamespace(
            cos_region=setting("REGION"),
            cos_secret_id=setting("SECRET_ID"),
            cos_secret_key=setting("SECRET_KEY"),
            cos_bucket=setting("BUCKET"),
            cos_prefix=setting("PREFIX"),
            cos_domain=setting("DOMAIN"),
            cos_timeout=120,
            profile="source",
        )
    )
    directory = Path(os.environ["PIPELINE_MEDIA_PREFLIGHT_TMP"])
    try:
        url = store_stream(sys.stdin.buffer, directory, store)
        print(json.dumps({"url": url}), flush=True)
    except Exception:
        print(json.dumps({"error": "tt_original_copy_failed"}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
