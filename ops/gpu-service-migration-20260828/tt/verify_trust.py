#!/usr/bin/env python3
"""Verify the private, unchanged US CA bundle without making network requests."""
from __future__ import annotations

import hashlib
import json
import os
import ssl
from pathlib import Path

TRUST = Path("/data/tt-post-gpu/trust")
CA_FILE = TRUST / "ca-bundle.pem"
CA_DIR = TRUST / "certs"
CA_SHA256 = "b6e66569cc3d438dd5abe514d0df50005d570bfc96c14dca8f768d020cb96171"
CA_BYTES = 226168
CA_COUNT = 145


def no_symlinks(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("trust path must be absolute and normalized")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("trust path or ancestor is a symlink")


def check_bundle(path: Path) -> dict:
    no_symlinks(path)
    if not path.is_file():
        raise ValueError("CA bundle is absent or not a regular file")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != CA_SHA256 or len(raw) != CA_BYTES:
        raise ValueError("CA bundle differs from the approved US source")
    if raw.count(b"-----BEGIN CERTIFICATE-----") != CA_COUNT:
        raise ValueError("CA certificate count differs")
    return {"sha256": sha, "bytes": len(raw), "pem_certificates": CA_COUNT}


def verify() -> dict:
    result = check_bundle(CA_FILE)
    no_symlinks(CA_DIR)
    if not CA_DIR.is_dir() or any(CA_DIR.iterdir()):
        raise ValueError("private CA directory must exist and remain empty")
    if (os.environ.get("SSL_CERT_FILE") != str(CA_FILE)
            or os.environ.get("SSL_CERT_DIR") != str(CA_DIR)):
        raise ValueError("SSL_CERT_FILE or SSL_CERT_DIR is not the approved private path")
    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or context.check_hostname is not True:
        raise ValueError("TLS certificate and hostname verification must remain enabled")
    stats = context.cert_store_stats()
    if stats.get("x509_ca", 0) < 1:
        raise ValueError("default TLS context did not load any CA certificates")
    return {"ok": True, **result, "loaded_ca_count": stats["x509_ca"],
            "cert_required": True, "check_hostname": True, "network_requests": 0}


if __name__ == "__main__":
    try:
        print(json.dumps(verify(), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else "private trust verification failed"}))
        raise SystemExit(1)
