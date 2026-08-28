#!/usr/bin/env python3
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
from urllib.request import Request, urlopen


HOST = os.environ.get("AD_MATERIAL_VISION_HOST", "127.0.0.1")
PORT = int(os.environ.get("AD_MATERIAL_VISION_PORT", "8796"))
WORK_ROOT = os.environ.get("AD_MATERIAL_VISION_WORK_ROOT", "/root/ad_material_vision_jobs")
CODEX_BIN = os.environ.get("AD_MATERIAL_VISION_CODEX_BIN", "/usr/bin/codex")
CODEX_MODEL = os.environ.get("AD_MATERIAL_VISION_CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
CODEX_REASONING = os.environ.get("AD_MATERIAL_VISION_CODEX_REASONING", "medium").strip() or "medium"
CODEX_TIMEOUT = int(os.environ.get("AD_MATERIAL_VISION_CODEX_TIMEOUT", "2400"))
SOURCE_CODEX_HOME = os.environ.get("AD_MATERIAL_VISION_SOURCE_CODEX_HOME", "/root/.codex")
ISOLATE_CODEX_HOME = os.environ.get("AD_MATERIAL_VISION_ISOLATE_CODEX_HOME", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)
MAX_CONCURRENCY = max(1, int(os.environ.get("AD_MATERIAL_VISION_MAX_CONCURRENCY", "1")))
REQUEST_BODY_LIMIT = int(os.environ.get("AD_MATERIAL_VISION_BODY_LIMIT", str(4 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = int(os.environ.get("AD_MATERIAL_VISION_DOWNLOAD_TIMEOUT", "90"))

SEMAPHORE = threading.Semaphore(MAX_CONCURRENCY)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)


def json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def toml_string(value):
    return json.dumps(str(value))


def prepare_isolated_codex_home(workdir):
    codex_home = os.path.join(workdir, "codex_home")
    if os.path.isdir(codex_home):
        shutil.rmtree(codex_home, ignore_errors=True)
    elif os.path.exists(codex_home):
        os.remove(codex_home)
    ensure_dir(codex_home)
    for dirname in ("generated_images", "sessions", "log", "tmp", "shell_snapshots"):
        ensure_dir(os.path.join(codex_home, dirname))
    with open(os.path.join(codex_home, "skills"), "w", encoding="utf-8") as fh:
        fh.write("system skills disabled for ad material vision worker\n")
    for filename in ("auth.json", "installation_id", "version.json", "models_cache.json", ".personality_migration"):
        source = os.path.join(SOURCE_CODEX_HOME, filename)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(codex_home, filename))
    config = "\n".join(
        [
            "model = %s" % toml_string(CODEX_MODEL),
            "model_reasoning_effort = %s" % toml_string(CODEX_REASONING),
            'approval_policy = "never"',
            "",
            "[projects.%s]" % toml_string(workdir),
            'trust_level = "trusted"',
            "",
            "[notice]",
            "hide_full_access_warning = true",
            "fast_default_opt_out = true",
            "",
        ]
    )
    with open(os.path.join(codex_home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write(config)
    return codex_home


def safe_slug(value):
    text = "".join(ch if ch.isalnum() else "_" for ch in str(value or "ref"))
    return text.strip("_")[:60] or "ref"


def download_reference(ref, index, workdir):
    url = str(ref.get("archive_url") or ref.get("url") or "").strip()
    if not url:
        raise ValueError("reference %s missing archive_url" % ref.get("asset_id"))
    suffix = Path(urlparse(url).path).suffix
    if not suffix or len(suffix) > 8:
        suffix = ".jpg"
    path = os.path.join(workdir, "%02d_%s%s" % (index, safe_slug(ref.get("asset_id")), suffix))
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        data = resp.read()
    if len(data) < 1024:
        raise ValueError("reference %s download too small" % ref.get("asset_id"))
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def extract_json_text(raw_text):
    text = (raw_text or "").strip()
    if not text:
        raise RuntimeError("Codex returned empty output")
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.startswith("```")]
        text = "\n".join(lines).strip()
    try:
        json.loads(text)
        return text
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        json.loads(candidate)
        return candidate
    raise RuntimeError("Codex output does not contain valid JSON")


def analyze(payload):
    prompt = str(payload.get("prompt") or "").strip()
    refs = payload.get("refs") or []
    if not prompt:
        raise ValueError("prompt is required")
    if not isinstance(refs, list) or not refs:
        raise ValueError("refs is required")
    job_id = safe_slug(payload.get("job_id") or str(uuid.uuid4()))
    workdir = os.path.join(WORK_ROOT, job_id)
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    ensure_dir(workdir)

    image_paths = [download_reference(ref, index + 1, workdir) for index, ref in enumerate(refs)]
    result_path = os.path.join(workdir, "codex_vision_result.json")
    stderr_path = os.path.join(workdir, "codex_vision_stderr.log")
    cmd = [
        CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        workdir,
        "-m",
        CODEX_MODEL,
        "-c",
        "model_reasoning_effort=%s" % toml_string(CODEX_REASONING),
    ]
    for image_path in image_paths:
        cmd.extend(["-i", image_path])
    cmd.extend(["-o", result_path, prompt])

    env = os.environ.copy()
    if ISOLATE_CODEX_HOME:
        env["CODEX_HOME"] = prepare_isolated_codex_home(workdir)
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=int(payload.get("timeout") or CODEX_TIMEOUT),
        env=env,
    )
    with open(stderr_path, "w", encoding="utf-8") as fh:
        fh.write((proc.stderr or "") + "\n" + (proc.stdout or ""))
    if proc.returncode != 0:
        raise RuntimeError("Codex vision failed (%s): %s" % (proc.returncode, (proc.stderr or proc.stdout or "")[-2000:]))
    if not os.path.isfile(result_path):
        raise RuntimeError("Codex did not write result JSON")
    return json.loads(extract_json_text(Path(result_path).read_text(encoding="utf-8")))


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "AdMaterialVisionService/1.0"

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self):
        if self.path == "/health":
            json_response(self, 200, {"ok": True, "service": "ad-material-vision"})
            return
        json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/api/ad-material-vision/analyze":
            json_response(self, 404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > REQUEST_BODY_LIMIT:
                raise ValueError("invalid request body length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            with SEMAPHORE:
                result = analyze(payload)
            json_response(self, 200, {"ok": True, "result": result})
        except Exception as exc:
            logging.exception("ad material vision failed")
            json_response(self, 500, {"ok": False, "error": str(exc)})


def main():
    ensure_dir(WORK_ROOT)
    server = ThreadedHTTPServer((HOST, PORT), Handler)
    logging.info("ad material vision service listening on %s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
