#!/usr/bin/env python3
"""Runs as an unprivileged user inside a per-image systemd mount namespace."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from common import atomic_json


def main():
    os.umask(0o077)
    work = Path(sys.argv[1]).resolve()
    spec = json.loads((work / "input.json").read_text(encoding="utf-8"))
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
           "--enable", "image_generation", "-m", spec["model"], "-c", "model_reasoning_effort=" + json.dumps(spec["reasoning"]),
           "-C", str(work), "-i", str(work / "reference.png"), "--json", "-o", str(work / "reply.json"), "-"]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(work), "CODEX_HOME": str(work / ".codex"), "LANG": "C.UTF-8", "TMPDIR": "/tmp"}
    receipt = {"ok": False, "code": "generation_failed", "message": "Codex 生图失败"}
    try:
        with open(work / "events.jsonl", "wb") as out, open(work / "stderr.log", "wb") as err:
            proc = subprocess.Popen(cmd, cwd=str(work), env=env, stdin=subprocess.PIPE, stdout=out, stderr=err, start_new_session=True)
            try:
                proc.communicate(instruction.encode("utf-8"), timeout=spec["timeout"])
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                receipt.update(code="generation_timeout", message="Codex 生图超时")
        # Discover only native tool artifacts from this fresh per-image Codex home.
        generated = work / ".codex" / "generated_images"
        candidates = []
        if generated.is_dir():
            for path in generated.rglob("*"):
                if path.is_file() and not path.is_symlink() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    candidates.append(path)
        if candidates:
            chosen = max(candidates, key=lambda p: p.stat().st_mtime_ns)
            receipt = {"ok": True, "generated_path": str(chosen.relative_to(work))}
        elif proc.returncode == 0:
            receipt.update(code="generation_no_image", message="Codex 已结束，但没有真实生图产物")
    except Exception:
        receipt.update(code="generation_failed", message="Codex 子进程执行失败")
    atomic_json(work / "receipt.json", receipt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
