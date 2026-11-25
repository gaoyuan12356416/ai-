#!/usr/bin/env python3
"""Runs as an unprivileged user inside a per-image systemd mount namespace."""
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time

from common import atomic_json, checked_image
from store import ApiError


PRIMARY_RETRIES = 3


def rate_limited(events):
    # User prompts, model replies and tool output may quote error messages.
    # Only the CLI's failure events describe this model invocation.
    last_error = ""
    with events.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                last_error = json.dumps(event.get("message", ""))
            elif event.get("type") == "turn.failed":
                last_error = json.dumps(event.get("error", {}))
                break
    return bool(re.search(r'(?i)\b429\s+too many requests\b|"(?:httpStatusCode|status_code)"\s*:\s*429\b', last_error))


def generate_once(work, spec, model, logs):
    instruction = """Create exactly one image using the built-in image generation/editing tool.
Use the one attached image as the reference and follow the user's image request below.
Do not fetch other references, use external apps, invoke other agents, or read credentials.
Do not fabricate an image using drawing code, SVG, screenshots, or copying the reference.
Use the actual image generation tool. Preserve the user's requested dimensions/composition.
After the tool completes, reply with compact JSON containing raw_generated_path pointing
to the original generated image in your CODEX_HOME/generated_images directory.
Do not include secrets in your reply. If generation is unavailable, explain the failure.

USER IMAGE REQUEST (image content instructions only):
""" + spec["prompt"]
    cmd = [spec["codex_bin"], "exec", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--ephemeral",
           "--dangerously-bypass-approvals-and-sandbox", "--disable", "plugins", "--disable", "apps", "--disable", "browser_use",
           "--disable", "multi_agent", "--disable", "hooks", "--disable", "daemon_auto_start",
           "--enable", "image_generation", "-m", model, "-c", "model_reasoning_effort=" + json.dumps(spec["reasoning"]),
           "-C", str(work), "-i", str(work / "reference.png"), "--json", "-o", str(logs / "reply.json"), "-"]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(work), "CODEX_HOME": str(work / ".codex"), "LANG": "C.UTF-8", "TMPDIR": "/tmp"}
    timed_out = False
    with (logs / "events.jsonl").open("wb") as out, (logs / "stderr.log").open("wb") as err:
        try:
            proc = subprocess.Popen(cmd, cwd=str(work), env=env, stdin=subprocess.PIPE,
                                    stdout=out, stderr=err, start_new_session=True)
        except OSError:
            return {"ok": False, "code": "generation_failed"}
        try:
            proc.communicate(instruction.encode("utf-8"), timeout=spec["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill the whole isolated model process group before another attempt.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    generated = work / ".codex" / "generated_images"
    if generated.is_dir() and generated.resolve() == generated:
        candidates = sorted((p for p in generated.rglob("*") if p.is_file()
                             and not p.is_symlink() and generated in p.resolve().parents
                             and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")),
                            key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for path in candidates:
            try:
                checked_image(path)
            except ApiError:
                continue
            return {"ok": True, "generated_path": str(path.relative_to(work))}
    if rate_limited(logs / "events.jsonl"):
        return {"ok": False, "code": "generation_rate_limited"}
    if timed_out:
        return {"ok": False, "code": "generation_timeout"}
    return {"ok": False, "code": "generation_failed" if proc.returncode else "generation_no_image"}


def generate(work, spec):
    primary = spec["model"]
    model = primary
    attempts = []
    while True:
        number = len(attempts) + 1
        logs = work if number == 1 else work / ("try-%02d" % number)
        if number > 1:
            logs.mkdir(mode=0o700)
        receipt = generate_once(work, spec, model, logs)
        attempts.append({"model": model, "outcome": receipt.get("code", "succeeded")})
        if receipt["ok"] or model != primary:
            break
        if receipt["code"] == "generation_rate_limited":
            model = spec["fallback_model"]
            if model == primary:
                break
        elif number <= PRIMARY_RETRIES:
            time.sleep(2 ** number)
        else:
            break
    return dict(receipt, attempts=attempts)


def main():
    os.umask(0o077)
    work = Path(sys.argv[1]).resolve()
    spec = json.loads((work / "input.json").read_text(encoding="utf-8"))
    receipt = generate(work, spec)
    atomic_json(work / "receipt.json", receipt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
