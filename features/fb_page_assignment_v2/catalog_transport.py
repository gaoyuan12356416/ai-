"""Read only fixed CPU publications over pinned SSH; install each artifact atomically."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .snapshot import DEFAULT_ROOT, HEX_HASH, atomic, canon, install, manifest

CPU_DIRECTORY = Path("/mnt/data-disk/fb-page-v2/catalog")
SOURCES = Path("/data/fb-page-assignment-v2/config/publishers.json")


def source_file(root, command):
    if command == "manifest":
        return root / "current.json"
    if command == "publishers":
        return root / "publishers.json"
    prefix = "archive:"
    if command.startswith(prefix) and HEX_HASH.fullmatch(command[len(prefix):]):
        return root / (command[len(prefix):] + ".json.gz")
    raise ValueError("unsupported_catalog_command")


def serve(root):
    with source_file(root, os.environ.get("SSH_ORIGINAL_COMMAND", "")).open("rb") as source:
        shutil.copyfileobj(source, sys.stdout.buffer)


def file_sha(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def pull_catalog(fetch, home):
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pull-", dir=home) as name:
        staging = Path(name)
        pointer = staging / "current.json"
        with pointer.open("wb") as dest:
            fetch("manifest", dest)
        head = manifest(pointer)
        current = home / "current.json"
        archive = head["sha256"] + ".json.gz"
        if current.exists() and (home / archive).exists():
            old = manifest(current)
            if (old["sha256"] == head["sha256"] and head.get("packed_sha256")
                    and file_sha(home / archive) == head["packed_sha256"]):
                return old
        with (staging / archive).open("wb") as dest:
            fetch("archive:" + head["sha256"], dest)
        return install(staging, home)


def pull_publishers(fetch, path):
    with tempfile.TemporaryFile() as fp:
        fetch("publishers", fp)
        fp.seek(0)
        data = fp.read(262145)
    if len(data) > 262144:
        raise ValueError("publisher_inventory_too_large")
    payload = json.loads(data)
    addresses = payload.get("addresses")
    if not isinstance(addresses, list):
        raise ValueError("invalid_publisher_inventory")
    for item in addresses:
        if not isinstance(item, str) or not ipaddress.ip_address(item).is_private:
            raise ValueError("invalid_publisher_address")
    value = canon(payload)
    if not path.is_file() or value != path.read_bytes():
        atomic(path, value)
    return len(addresses)


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    source = actions.add_parser("serve")
    source.add_argument("--root", default=CPU_DIRECTORY, type=Path)
    pull = actions.add_parser("pull")
    pull.add_argument("--home", default=Path(DEFAULT_ROOT), type=Path)
    pull.add_argument("--publishers", default=SOURCES, type=Path)
    pull.add_argument("--host", default="root@43.166.187.96")
    pull.add_argument("--key", default="/etc/fb-page-assignment-v2/catalog_ed25519")
    pull.add_argument("--known-hosts", default="/etc/ai-backend-offload/known_hosts")
    pull.add_argument("--only", choices=("catalog", "publishers"))
    args = parser.parse_args()
    if args.action == "serve":
        serve(args.root)
        return
    def fetch(command, dest):
        subprocess.run(["ssh", "-T", "-n", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                        "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
                        "-o", "ServerAliveInterval=8", "-o", "ServerAliveCountMax=3",
                        "-o", "UserKnownHostsFile=" + args.known_hosts, "-i", args.key,
                        args.host, command], stdout=dest, check=True, timeout=65)
    failures = []
    if not args.only or args.only == "catalog":
        try:
            print(json.dumps(pull_catalog(fetch, args.home)))
        except (OSError, ValueError, KeyError, EOFError, subprocess.SubprocessError) as exc:
            failures.append(("catalog", type(exc).__name__))
    if not args.only or args.only == "publishers":
        try:
            print("publisher_addresses", pull_publishers(fetch, args.publishers))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            failures.append(("publishers", type(exc).__name__))
    if failures:
        raise SystemExit("pull failed " + str(failures))


if __name__ == "__main__":
    main()
