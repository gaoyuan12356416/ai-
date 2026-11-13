#!/usr/bin/env python3
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import time

from common import Config, CosUploader, atomic_json, checked_image, download_image, sha256
from store import ApiError, Store


def unit_name(row):
    return "codex-image-job-" + row["attempt_id"] + ".service"


def unit_state(name):
    p = subprocess.run(["systemctl", "show", name, "--property=LoadState,ActiveState,SubState,MainPID"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15)
    values = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
    if p.returncode and not values:
        # An unavailable service manager is not evidence that a running model is dead.
        raise RuntimeError("cannot read generation unit")
    return values


def active_unit(values):
    return values.get("ActiveState") in ("active", "activating", "deactivating", "reloading") or int(values.get("MainPID") or "0") > 0


class Worker:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.stop = False

    def stop_requested(self, *args):
        self.stop = True

    def directory(self, row):
        return self.config.root / "jobs" / row["task_id"] / ("%06d" % row["ordinal"])

    def launch(self, row, directory):
        user = pwd.getpwnam(self.config.runner_user)
        run = directory / row["attempt_id"]
        run.mkdir(mode=0o700, exist_ok=True)
        # Parent directories allow traversal only; other jobs and the ledger stay private.
        for parent in (self.config.root, self.config.root / "jobs", directory.parent, directory):
            os.chmod(parent, 0o711)
        from PIL import Image
        with Image.open(directory / "reference.download") as image:
            image.convert("RGBA" if "A" in image.getbands() else "RGB").save(run / "reference.png")
        home = run / ".codex"
        home.mkdir(mode=0o700)
        (home / "skills").mkdir()
        source_auth = self.config.source_home / "auth.json"
        lock = open("/tmp/codex_screenshot_auth_sync.lock", "a+")
        with lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            auth = source_auth.read_bytes()
            (home / "auth.json").write_bytes(auth)
            for filename in ("installation_id", "version.json", "models_cache.json"):
                source = self.config.source_home / filename
                if source.is_file():
                    shutil.copyfile(source, home / filename)
        atomic_json(directory / "auth-base.json", {"sha256": hashlib.sha256(auth).hexdigest()})
        atomic_json(run / "input.json", {"prompt": row["prompt"], "model": self.config.model, "reasoning": self.config.reasoning, "timeout": self.config.timeout, "codex_bin": self.config.codex_bin})
        for path in [run] + list(run.rglob("*")):
            os.chown(path, user.pw_uid, user.pw_gid)
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        # Write intent before spawning. A crash here must never blindly launch a second model.
        self.store.update(row, "generating")
        atomic_json(directory / "launch.json", {"unit": unit_name(row), "started_at": time.time(), "attempt_id": row["attempt_id"]})
        runner = str(Path(__file__).resolve().with_name("runner.py"))
        cmd = ["systemd-run", "--quiet", "--collect", "--unit=" + unit_name(row),
               "--property=User=" + self.config.runner_user, "--property=Group=" + self.config.runner_user,
               "--property=NoNewPrivileges=yes", "--property=ProtectHome=yes", "--property=ProtectSystem=strict",
               "--property=PrivateTmp=yes", "--property=PrivateDevices=yes",
               "--property=CapabilityBoundingSet=", "--property=TemporaryFileSystem=/mnt:ro",
               "--property=BindReadOnlyPaths=" + str(Path(__file__).resolve().parent),
               "--property=BindPaths=" + str(run), "--property=ReadWritePaths=" + str(run),
               # systemd 239 uses this mask for mount-namespace parent directories.
               # The pre-created task is 0700; runner.py sets 0077 before any writes.
               "--property=WorkingDirectory=" + str(run), "--property=UMask=0022",
               "--property=MemoryMax=3G", "--property=TasksMax=128",
               "--property=RuntimeMaxSec=" + str(self.config.timeout + 90),
               "--property=StandardOutput=null", "--property=StandardError=journal",
               "/usr/bin/python3", runner, str(run)]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        if result.returncode:
            # Keep non-secret launcher diagnostics private for support.
            (directory / "launcher-error.txt").write_text(result.stderr[-4000:], encoding="utf-8")
            if active_unit(unit_state(unit_name(row))) or (run / "receipt.json").is_file():
                return
            raise ApiError("generation_launch_failed", "生图子进程启动失败")

    def sync_auth(self, directory, run):
        base = directory / "auth-base.json"
        source = self.config.source_home / "auth.json"
        generated = run / ".codex" / "auth.json"
        if not base.exists() or not generated.is_file() or generated.is_symlink():
            return
        # Only merge a refreshed credential if no other service has changed the source.
        with open("/tmp/codex_screenshot_auth_sync.lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            original = json.loads(base.read_text())["sha256"]
            refreshed = generated.read_bytes()
            if sha256(source) == original and hashlib.sha256(refreshed).hexdigest() != original:
                value = json.loads(refreshed)
                atomic_json(source, value)

    def collect(self, row, directory):
        run = directory / row["attempt_id"]
        if active_unit(unit_state(unit_name(row))):
            return False
        try:
            self.sync_auth(directory, run)
        except Exception:
            logging.warning("auth merge skipped for %s/%d", row["task_id"], row["ordinal"])
        receipt_path = run / "receipt.json"
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise ApiError("execution_interrupted", "生图进程已退出且无完整回执，未自动重新生成")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError()
        except (ValueError, OSError):
            raise ApiError("invalid_output", "生图回执无法解析，未自动重新生成")
        if not receipt.get("ok"):
            allowed = {"generation_failed", "generation_timeout", "generation_no_image"}
            code = receipt.get("code") if receipt.get("code") in allowed else "generation_failed"
            messages = {"generation_failed": "Codex 生图失败", "generation_timeout": "Codex 生图超时", "generation_no_image": "Codex 未返回真实生图产物"}
            raise ApiError(code, messages[code])
        generated_root = (run / ".codex" / "generated_images").resolve()
        if not isinstance(receipt.get("generated_path"), str):
            raise ApiError("invalid_output", "生图回执缺少图片路径")
        candidate = run / receipt["generated_path"]
        resolved = candidate.resolve()
        if candidate.is_symlink() or generated_root not in resolved.parents:
            raise ApiError("invalid_output", "生图结果路径无效")
        checked_image(resolved)
        from PIL import Image
        output = directory / "result.png"
        with Image.open(resolved) as im:
            im.convert("RGBA" if "A" in im.getbands() else "RGB").save(directory / "result.tmp.png")
        os.replace(directory / "result.tmp.png", output)
        atomic_json(directory / "generated.json", {"sha256": sha256(output), "attempt_id": row["attempt_id"]})
        return True

    def process(self, row, fresh):
        directory = self.directory(row)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            # This checkpoint is root-owned, outside the child namespace.
            if not (directory / "generated.json").exists():
                if row["stage"] == "preparing":
                    download_image(row["image_url"], directory / "reference.download")
                    self.launch(row, directory)
                while not self.collect(row, directory):
                    if self.stop:
                        return
                    time.sleep(2)
            output = directory / "result.png"
            checkpoint = json.loads((directory / "generated.json").read_text())
            if checkpoint["attempt_id"] != row["attempt_id"] or sha256(output) != checkpoint["sha256"]:
                raise ApiError("invalid_output", "已生成图片的检查记录不匹配")
            self.store.update(row, "uploading")
            url = CosUploader(self.config).upload(row, output)
            self.store.update(row, "succeeded", state="succeeded", cos_url=url)
        except ApiError as exc:
            self.store.update(row, "failed", state="failed", error={"code": exc.code, "message": exc.message})
        except Exception:
            logging.exception("worker item error for %s/%d", row["task_id"], row["ordinal"])
            try:
                # An unavailable manager or live process retains its claim. A confirmed
                # dead item with a local error must not indefinitely block later images.
                if not active_unit(unit_state(unit_name(row))):
                    self.store.update(row, "failed", state="failed", error={"code": "processing_failed", "message": "该图片处理异常，已保留执行记录，未自动重新生成"})
                    return
            except Exception:
                logging.warning("image claim retained pending reconciliation")
            time.sleep(5)

    def run(self):
        self.config.root.mkdir(parents=True, exist_ok=True)
        with open(self.config.root / "worker.lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            while not self.stop:
                row, fresh = self.store.next_image()
                if row:
                    self.process(row, fresh)
                else:
                    time.sleep(1)


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config()
    store = Store(config.root / "state.sqlite3")
    store.initialize()
    worker = Worker(config, store)
    signal.signal(signal.SIGTERM, worker.stop_requested)
    signal.signal(signal.SIGINT, worker.stop_requested)
    worker.run()


if __name__ == "__main__":
    main()
