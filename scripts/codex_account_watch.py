#!/usr/bin/env python3
"""Keep two private Codex logins; reset the new account before falling back.

Only official app-server account methods are used. No inference is started.
Running Codex sessions are never interrupted or logged out.
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import uuid


class RpcError(RuntimeError):
    pass


class Deferred(RuntimeError):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def auth_identity(auth):
    tokens = auth.get("tokens") or {}
    account = tokens.get("account_id")
    subject = None
    for name in ("id_token", "access_token"):
        try:
            payload = tokens[name].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            subject = claims.get("sub")
            if subject:
                break
        except (KeyError, ValueError, IndexError, TypeError):
            continue
    if not account or not subject:
        raise ValueError("ChatGPT account identity is missing from the private auth cache")
    return hashlib.sha256((str(account) + "\0" + str(subject)).encode()).hexdigest()


def summarize_usage(result):
    bucket = (result.get("rateLimitsByLimitId") or {}).get("codex")
    if bucket is None:
        bucket = result.get("rateLimits")
        if bucket and bucket.get("limitId") not in (None, "codex"):
            bucket = None
    windows = []
    for name in ("primary", "secondary"):
        window = (bucket or {}).get(name)
        if not window:
            continue
        used = window.get("usedPercent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
            continue
        windows.append({"window": name, "remainingPercent": max(0.0, 100.0 - used),
                        "windowDurationMins": window.get("windowDurationMins"),
                        "resetsAt": window.get("resetsAt")})
    credits = result.get("rateLimitResetCredits")
    count = credits.get("availableCount") if isinstance(credits, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = None
    return {"remainingPercent": min((w["remainingPercent"] for w in windows), default=None),
            "windows": windows, "resetCredits": count,
            "planType": (bucket or {}).get("planType")}


def choose_credit(result, now=None):
    now = time.time() if now is None else now
    credits = (result.get("rateLimitResetCredits") or {}).get("credits") or []
    eligible = [c for c in credits if c.get("status") == "available"
                and c.get("resetType") == "codexRateLimits" and c.get("id")
                and (c.get("expiresAt") is None or c["expiresAt"] > now)]
    return min(eligible, key=lambda c: c.get("expiresAt") or float("inf"))["id"] if eligible else None


class AppServer:
    def __init__(self, home, binary="/usr/bin/codex", timeout=25):
        self.timeout = timeout
        self.serial = 0
        self.messages = queue.Queue()
        self.proc = subprocess.Popen(
            [binary, "-c", 'cli_auth_credentials_store="file"', "app-server", "--listen", "stdio://"],
            env={**os.environ, "CODEX_HOME": str(home)}, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            start_new_session=True)
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "cpu_codex_account_watch", "version": "1.0.0"},
                                     "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _reader(self):
        try:
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            self.messages.put(None)

    def _send(self, value):
        self.proc.stdin.write(json.dumps(value) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params=None):
        self.serial += 1
        request_id = self.serial
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                reply = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(method + " timed out") from None
            if reply is None:
                raise RpcError("app-server exited while handling " + method)
            if reply.get("id") != request_id:
                continue
            if "error" in reply:
                # Upstream errors may include request details; never log raw payloads.
                raise RpcError(method + " failed (code " + str(reply["error"].get("code")) + ")")
            return reply.get("result") or {}

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def source_writers(active_home, proc_root=Path("/proc")):
    """Find only screenshot handlers that can write back this exact auth cache."""
    writers = []
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            args = (process / "cmdline").read_bytes().split(b"\0")
            if not any(arg.endswith(b"/codex_screenshot_service.py") or arg == b"codex_screenshot_service.py" for arg in args[:4]):
                continue
            env = dict(entry.split("=", 1) for entry in (process / "environ").read_text().split("\0") if "=" in entry)
            truth = lambda key, default: env.get(key, default).strip().lower() in ("1", "true", "yes")
            source = Path(env.get("CODEX_SCREENSHOT_SOURCE_CODEX_HOME", "/root/.codex"))
            if not truth("CODEX_SCREENSHOT_ISOLATE_CODEX_HOME", "0") or not truth("CODEX_SCREENSHOT_SYNC_ISOLATED_AUTH", "1"):
                continue
            if source.resolve() != active_home.resolve():
                continue
            writers.append({"pid": process.name, "threads": len(list((process / "task").iterdir())),
                            "lock": env.get("CODEX_SCREENSHOT_AUTH_SYNC_LOCK_PATH", "/tmp/codex_screenshot_auth_sync.lock")})
        except (OSError, ValueError):
            if process.exists():
                raise Deferred("cannot inspect a credential-writing process") from None
    return writers


@contextmanager
def credential_switch_window(active_home):
    """Do not interrupt jobs or race an old isolated job's credential writeback."""
    import fcntl
    locks = []
    try:
        writers = source_writers(active_home)
        paths = {"/tmp/codex_screenshot_auth_sync.lock"} | {w["lock"] for w in writers}
        for name in sorted(paths):
            stream = open(name, "a+")
            locks.append(stream)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Deferred("credential sync is busy") from None
        current = source_writers(active_home)
        if {w["lock"] for w in current} - paths:
            raise Deferred("credential writer configuration changed")
        busy = [w["pid"] for w in current if w["threads"] > 1]
        if busy:
            raise Deferred("waiting for credential-writing request handlers: " + ",".join(busy))
        yield
    finally:
        for stream in reversed(locks):
            stream.close()


class Watch:
    def __init__(self, root, active_home, client_factory=AppServer):
        self.root = Path(root)
        self.active_home = Path(active_home)
        self.state_path = self.root / "state.json"
        self.client_factory = client_factory
        self.state = read_json(self.state_path)

    def save(self):
        self.state["updatedAt"] = int(time.time())
        atomic_json(self.state_path, self.state)

    def event(self, event, **fields):
        row = {"at": int(time.time()), "event": event, **fields}
        self.state["lastEvent"] = row
        self.save()
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def home(self, account):
        return self.root / "accounts" / account / "home"

    def expected(self, account):
        return self.state["identities"][account]

    def usage(self, home, expected):
        if auth_identity(read_json(home / "auth.json")) != expected:
            raise ValueError("credential identity does not match the registered account")
        with self.client_factory(home) as client:
            account = client.call("account/read", {"refreshToken": False}).get("account")
            if not account or account.get("type") != "chatgpt":
                raise ValueError("the account is not authenticated with ChatGPT")
            result = client.call("account/rateLimits/read")
        if auth_identity(read_json(home / "auth.json")) != expected:
            raise ValueError("credential identity changed during usage readback")
        return result

    def switch(self, destination, reason):
        current = self.state["active"]
        candidate_home = self.home(destination)
        candidate = self.usage(candidate_home, self.expected(destination))
        summary = summarize_usage(candidate)
        if summary["remainingPercent"] is None or summary["remainingPercent"] <= self.state["thresholdPercent"]:
            self.event("destination_unavailable", destination=destination, usage=summary)
            return False
        with credential_switch_window(self.active_home):
            current_auth = read_json(self.active_home / "auth.json")
            if auth_identity(current_auth) != self.expected(current):
                raise ValueError("active credential was changed outside this watch")
            next_auth = read_json(candidate_home / "auth.json")
            if auth_identity(next_auth) != self.expected(destination):
                raise ValueError("destination credential identity changed")
            # Save the latest refreshed original credential immediately before handoff.
            atomic_json(self.home(current) / "auth.json", current_auth)
            self.state["pendingSwitch"] = {"from": current, "to": destination, "reason": reason}
            self.save()
            atomic_json(self.active_home / "auth.json", next_auth)
            self.state["active"] = destination
            self.state["phase"] = "watching" if destination == "new" else "fallback_complete"
            self.state.pop("pendingSwitch", None)
            self.state.pop("pendingReset", None)
            self.event("account_switched", account=destination, reason=reason, usage=summary,
                       existingSessions="unchanged_until_reconnect")
        return True

    def reconcile_switch(self):
        pending = self.state.get("pendingSwitch")
        if not pending:
            return
        identity = auth_identity(read_json(self.active_home / "auth.json"))
        if identity == self.expected(pending["to"]):
            self.state["active"] = pending["to"]
            self.state["phase"] = "watching" if pending["to"] == "new" else "fallback_complete"
            self.state.pop("pendingReset", None)
            self.state.pop("pendingSwitch")
            self.event("switch_reconciled", account=self.state["active"])
        elif identity == self.expected(pending["from"]):
            self.state.pop("pendingSwitch")
            self.save()
        else:
            raise ValueError("pending switch has an unknown credential identity")

    def tick(self):
        self.reconcile_switch()
        active = self.state["active"]
        if self.state["phase"] == "waiting_new_login":
            auth_path = self.home("new") / "auth.json"
            if not auth_path.exists():
                self.event("waiting_new_login")
                return
            identity = auth_identity(read_json(auth_path))
            if identity == self.expected("original"):
                self.event("new_login_is_original_account")
                return
            self.state["identities"]["new"] = identity
            self.save()
            self.switch("new", "new_login_verified")
            return
        usage = self.usage(self.active_home, self.expected(active))
        summary = summarize_usage(usage)
        self.state["lastUsage"] = {"account": active, "at": int(time.time()), **summary}
        self.save()
        if self.state["phase"] == "fallback_complete":
            self.event("fallback_active", usage=summary)
            return
        remaining = summary["remainingPercent"]
        if remaining is None:
            self.event("usage_unknown")
            return
        if remaining > self.state["thresholdPercent"]:
            self.state.pop("pendingReset", None)
            self.state.pop("resetNotEligibleAt", None)
            self.event("new_account_healthy", usage=summary)
            return
        pending = self.state.get("pendingReset")
        if pending and pending.get("outcome") in ("reset", "alreadyRedeemed"):
            # A successful reset must be visible in the official usage readback.
            # Never consume another card merely because propagation is delayed.
            self.event("awaiting_reset_readback", usage=summary)
            return
        if summary["resetCredits"] == 0 and not pending:
            self.switch("original", "new_low_and_no_reset_credit")
            return
        if summary["resetCredits"] is None and not pending:
            self.event("reset_credit_count_unknown", usage=summary)
            return
        usage_signature = {"remainingPercent": remaining, "windows": summary["windows"]}
        if not pending and self.state.get("resetNotEligibleAt") == usage_signature:
            self.event("reset_not_yet_eligible", usage=summary)
            return
        if not pending:
            self.state.pop("resetNotEligibleAt", None)
            pending = {"idempotencyKey": str(uuid.uuid4()), "startedAt": int(time.time())}
            credit = choose_credit(usage)
            if credit:
                pending["creditId"] = credit
            self.state["pendingReset"] = pending
            self.save()
        params = {k: pending[k] for k in ("idempotencyKey", "creditId") if k in pending}
        # Retries always keep the same key, including after timeout or service restart.
        with self.client_factory(self.active_home) as client:
            outcome = client.call("account/rateLimitResetCredit/consume", params).get("outcome")
        pending["outcome"] = outcome
        self.save()
        if outcome == "noCredit":
            self.state.pop("pendingReset", None)
            self.save()
            self.switch("original", "official_reset_no_credit")
        elif outcome in ("reset", "alreadyRedeemed"):
            updated = summarize_usage(self.usage(self.active_home, self.expected(active)))
            if updated["remainingPercent"] is not None and updated["remainingPercent"] > self.state["thresholdPercent"]:
                self.state.pop("pendingReset", None)
                self.event("reset_verified", usage=updated)
            else:
                self.event("awaiting_reset_readback", usage=updated)
        elif outcome == "nothingToReset":
            # This definitive reply did not spend a card. Retry with a new key
            # only when the quota changes (some accounts require 0% remaining).
            self.state.pop("pendingReset", None)
            self.state["resetNotEligibleAt"] = usage_signature
            self.event("reset_not_yet_eligible", usage=summary)
        else:
            self.event("reset_outcome_unknown")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/mnt/data-disk/codex-accounts"))
    parser.add_argument("--active-home", type=Path, default=Path("/root/.codex"))
    parser.add_argument("command", choices=("init", "tick", "status", "return-original"))
    args = parser.parse_args()
    import fcntl
    args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.root / "watch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"event":"watch_already_running"}')
            return 0
        if args.command == "init":
            if (args.root / "state.json").exists():
                raise ValueError("state already exists; refusing to overwrite it")
            original = read_json(args.active_home / "auth.json")
            identity = auth_identity(original)
            atomic_json(args.root / "accounts/original/home/auth.json", original)
            atomic_json(args.root / "state.json", {"version": 1, "active": "original", "phase": "waiting_new_login",
                                                   "thresholdPercent": 1, "identities": {"original": identity}})
            print('{"event":"initialized_waiting_new_login"}')
            return 0
        watch = Watch(args.root, args.active_home)
        if args.command == "status":
            safe = {k: v for k, v in watch.state.items() if k != "identities"}
            if "pendingReset" in safe:
                safe["pendingReset"] = {k: v for k, v in safe["pendingReset"].items() if k != "creditId"}
            print(json.dumps(safe, ensure_ascii=False, indent=2))
            return 0
        try:
            if args.command == "tick":
                watch.tick()
            elif watch.state["active"] != "original":
                watch.switch("original", "operator_return_original")
        except Deferred as exc:
            watch.event("switch_deferred", reason=str(exc))
        except Exception as exc:
            watch.event("check_failed", errorType=type(exc).__name__)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
