#!/usr/bin/env python3
"""Extend the existing HK-only tunnel key by four loopback listeners, never shell access."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import socket
import time

from maintenance import BASE, RUN_ID, atomic_write, run, storage_guard

KEY_FP = "SHA256:+nkbj63n9YgoP++S+2f+B9O6GLrAlzoHLIWeBKRJnhg"
OLD_PORTS = (18820, 18788, 18836)
NEW_PORTS = (18796, 18797, 18830, 18834)
KEYS = pathlib.Path("/root/.ssh/authorized_keys")


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(key)).digest()).decode().rstrip("=")


def rewrite(text, rollback=False, expected_fingerprint=KEY_FP):
    lines = text.splitlines()
    matches = []
    for index, line in enumerate(lines):
        if " ssh-ed25519 " in line and not line.lstrip().startswith("#"):
            options, key_part = line.split(" ssh-ed25519 ", 1)
            if fingerprint(key_part.split()[0]) == expected_fingerprint:
                matches.append((index, options, key_part))
    if len(matches) != 1:
        raise RuntimeError("expected exactly one pinned HK tunnel public key")
    index, options, key_part = matches[0]
    options = options.split(",")
    required = {'command="/usr/bin/sleep infinity"', 'from="43.154.250.89"',
                "restrict", "port-forwarding"}
    if not required.issubset(set(options)):
        raise RuntimeError("existing HK tunnel key restrictions changed")
    listeners = {o for o in options if o.startswith("permitlisten=")}
    expected = {'permitlisten="127.0.0.1:%d"' % p for p in OLD_PORTS}
    added = {'permitlisten="127.0.0.1:%d"' % p for p in NEW_PORTS}
    if listeners not in (expected, expected | added):
        raise RuntimeError("HK listener set drifted; refuse broad authorization edit")
    if rollback:
        options = [o for o in options if o not in added]
    else:
        options.extend('permitlisten="127.0.0.1:%d"' % p for p in NEW_PORTS
                       if 'permitlisten="127.0.0.1:%d"' % p not in options)
    lines[index] = ",".join(options) + " ssh-ed25519 " + key_part
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("extend", "rollback"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if socket.gethostname() != "VM-0-108-centos":
        raise RuntimeError("CPU-only authorization change")
    storage_guard()
    before = KEYS.read_text()
    after = rewrite(before, rollback=args.action == "rollback")
    receipt = {"run_id": RUN_ID, "action": args.action, "key_fingerprint": KEY_FP,
               "source_host": "43.154.250.89", "new_loopback_ports": NEW_PORTS,
               "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
               "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
               "checked_at_epoch": time.time(), "changed": before != after}
    if not args.apply:
        receipt["dry_run"] = True
        print(json.dumps(receipt))
        return
    run(["/usr/sbin/sshd", "-t"])
    backup = BASE / "hk-authorized-keys-before.txt"
    if args.action == "extend" and not backup.exists():
        atomic_write(backup, before)
    if KEYS.read_text() != before:
        raise RuntimeError("authorized_keys concurrently changed")
    atomic_write(KEYS, after, 0o600)
    atomic_write(BASE / ("hk-tunnel-" + args.action + ".json"), json.dumps(receipt, indent=2))
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
