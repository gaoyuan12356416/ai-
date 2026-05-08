#!/usr/bin/env python3
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlparse

import requests
from PIL import ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

HOST = os.environ.get("CODEX_SCREENSHOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_SCREENSHOT_PORT", "8791"))
WORK_ROOT = os.environ.get("CODEX_SCREENSHOT_WORK_ROOT", "/root/codex_screenshot_jobs")
PUBLIC_ROOT = os.environ.get(
    "CODEX_SCREENSHOT_PUBLIC_ROOT", "/usr/share/nginx/html/drama-screenshot-materials"
)
PUBLIC_BASE_URL = os.environ.get(
    "CODEX_SCREENSHOT_PUBLIC_BASE_URL", "https://ai.yingliangads.com/drama-screenshot-materials"
)
PROMPT_WORKSPACE_ROOT = os.environ.get(
    "CODEX_SCREENSHOT_PROMPT_WORKSPACE", "/root/codex_screenshot_worker_workspace"
)
CODEX_BIN = os.environ.get("CODEX_SCREENSHOT_CODEX_BIN", "/usr/bin/codex")
CODEX_TIMEOUT = int(os.environ.get("CODEX_SCREENSHOT_CODEX_TIMEOUT", "1800"))
MAX_CONCURRENCY = max(1, int(os.environ.get("CODEX_SCREENSHOT_MAX_CONCURRENCY", "1")))
GENERATION_SEMAPHORE = threading.Semaphore(MAX_CONCURRENCY)

# Cache: avoids repeated generations for same source + spec + prompt version.
CACHE_ROOT = os.environ.get("CODEX_SCREENSHOT_CACHE_ROOT", "/root/codex_screenshot_cache")
PROMPT_VERSION = os.environ.get("CODEX_SCREENSHOT_PROMPT_VERSION", "v1")
KEEP_JOB_WORKSPACE = os.environ.get("CODEX_SCREENSHOT_KEEP_JOB_WORKSPACE", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
)


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


def run_cmd(cmd, timeout=None):
    logging.info("running: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "command failed (%s): %s"
            % (proc.returncode, proc.stderr.strip() or proc.stdout.strip())
        )
    return proc


def build_public_url(path):
    rel_path = os.path.relpath(path, PUBLIC_ROOT).replace(os.sep, "/")
    return PUBLIC_BASE_URL.rstrip("/") + "/" + rel_path


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


def prepare_source_file(source_path, source_url, workdir):
    source_ext = ".jpg"
    if source_path and os.path.isfile(source_path):
        source_ext = os.path.splitext(source_path)[1] or ".jpg"
        staged_path = os.path.join(workdir, "source%s" % source_ext)
        shutil.copy2(source_path, staged_path)
        return staged_path
    if not source_url:
        raise ValueError("source_path or source_url is required")
    staged_path = os.path.join(workdir, "source.jpg")
    response = requests.get(source_url, stream=True, timeout=180)
    response.raise_for_status()
    with open(staged_path, "wb") as fh:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fh.write(chunk)
    return staged_path


def cache_key(source_url, drama_name, item):
    parts = [
        "prompt=" + PROMPT_VERSION,
        "src=" + (source_url or "").strip(),
        "title=" + (drama_name or "").strip(),
        "key=" + str(item.get("key", "")).strip(),
        "ratio=" + str(item.get("ratio", "")).strip(),
        "w=" + str(int(item.get("width") or 0)),
        "h=" + str(int(item.get("height") or 0)),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def try_restore_from_cache(source_url, drama_name, item):
    key = cache_key(source_url, drama_name, item)
    cached_dir = os.path.join(CACHE_ROOT, key)
    cached_img = os.path.join(cached_dir, "output.jpg")
    if not os.path.isfile(cached_img):
        return None
    workspace_output_path = str(item.get("workspace_output_path", "")).strip()
    public_output_path = str(item.get("public_output_path", "")).strip()
    ensure_dir(os.path.dirname(workspace_output_path))
    ensure_dir(os.path.dirname(public_output_path))
    shutil.copy2(cached_img, workspace_output_path)
    shutil.copy2(cached_img, public_output_path)
    return {
        "key": str(item.get("key", "")).strip(),
        "status": "done",
        "workspace_output_path": workspace_output_path,
        "public_output_path": public_output_path,
        "public_url": build_public_url(public_output_path),
        "generator": "codex-imagegen",
        "cache": "hit",
        "cache_key": key,
    }


def store_to_cache(source_url, drama_name, item):
    key = cache_key(source_url, drama_name, item)
    cached_dir = os.path.join(CACHE_ROOT, key)
    ensure_dir(cached_dir)
    workspace_output_path = str(item.get("workspace_output_path", "")).strip()
    cached_img = os.path.join(cached_dir, "output.jpg")
    if os.path.isfile(workspace_output_path):
        shutil.copy2(workspace_output_path, cached_img)
    return key


def job_workspace(job_id):
    # Each job runs in a clean workspace to avoid stale files inflating token usage.
    return os.path.join(PROMPT_WORKSPACE_ROOT, "_jobs", job_id)


def reset_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    ensure_dir(path)


def build_codex_instruction(drama_name, item):
    title = (drama_name or "").strip() or "the provided drama"
    return (
        "Use the attached original drama cover as the source image. "
        "Create a new finished paid-social key art image for {title} at exactly {width}x{height} pixels, aspect ratio {ratio}. "
        "Keep the main characters, faces, costumes, title text, logos, and visual identity recognizable from the original source, while adapting the composition naturally to the target canvas. "
        "Extend or recreate the surrounding background as needed so the result looks complete, polished, and not like a cropped or stretched image. "
        "Do not add watermarks, unrelated props, extra people, duplicate limbs, deformed hands, or collage seams. "
        "After generation, copy the selected final image into {output_path}. "
        "Reply with only compact JSON containing output_path and summary."
    ).format(
        ratio=item["ratio"],
        width=int(item["width"]),
        height=int(item["height"]),
        title=title,
        output_path=item["workspace_output_path"],
    )


def build_codex_batch_instruction(drama_name, items):
    title = (drama_name or "").strip() or "the provided drama"
    specs = []
    for item in items:
        specs.append(
            {
                "key": str(item.get("key", "")).strip(),
                "ratio": str(item.get("ratio", "")).strip(),
                "width": int(item.get("width") or 0),
                "height": int(item.get("height") or 0),
                "output_path": str(item.get("workspace_output_path", "")).strip(),
            }
        )
    return (
        "Use the attached original drama cover as the source image. "
        "Create finished paid-social key art images for {title} for every requested canvas below. "
        "Keep the main characters, faces, costumes, title text, logos, and visual identity recognizable from the original source across all outputs. "
        "Adapt each composition naturally to its target canvas, extending or recreating the surrounding background as needed so each image looks complete, polished, and not cropped or stretched. "
        "Do not add watermarks, unrelated props, extra people, duplicate limbs, deformed hands, or collage seams. "
        "Requested outputs: {specs}. "
        "After generation, copy each selected final image into its exact output_path. "
        "Reply with only compact JSON containing an items array with key, output_path, and summary."
    ).format(title=title, specs=json.dumps(specs, ensure_ascii=False, separators=(",", ":")))


def generate_screenshots(payload):
    job_id = str(payload.get("job_id", "")).strip()
    source_path = str(payload.get("source_path", "")).strip()
    source_url = str(payload.get("source_url", "")).strip()
    drama_name = str(payload.get("drama_name", "")).strip()
    items = payload.get("items", [])
    if not job_id:
        raise ValueError("job_id is required")
    if not isinstance(items, list) or not items:
        raise ValueError("items is required")

    workdir = os.path.join(WORK_ROOT, job_id)
    ensure_dir(workdir)
    ensure_dir(CACHE_ROOT)
    ensure_dir(PROMPT_WORKSPACE_ROOT)

    staged_source_path = prepare_source_file(source_path, source_url, workdir)

    # Workspace isolation (2): clean per job.
    ws = job_workspace(job_id)
    reset_dir(ws)

    results = []
    remaining = []

    # Cache (3): restore any items we already have.
    for item in items:
        hit = try_restore_from_cache(source_url, drama_name, item)
        if hit:
            results.append(hit)
            continue
        remaining.append(item)

    if len(remaining) > 1:
        for item in remaining:
            workspace_output_path = str(item.get("workspace_output_path", "")).strip()
            public_output_path = str(item.get("public_output_path", "")).strip()
            ensure_dir(os.path.dirname(workspace_output_path))
            ensure_dir(os.path.dirname(public_output_path))
            if workspace_output_path and os.path.exists(workspace_output_path):
                os.remove(workspace_output_path)

        result_json_path = os.path.join(workdir, "batch_result.json")
        cmd = [
            CODEX_BIN,
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            ws,
            "-i",
            staged_source_path,
            "-o",
            result_json_path,
            build_codex_batch_instruction(drama_name, remaining),
        ]
        try:
            run_cmd(cmd, timeout=CODEX_TIMEOUT)
        except Exception:
            logging.exception("batch generation failed for %s", job_id)

        batch_result_data = {"items": []}
        if os.path.isfile(result_json_path):
            try:
                with open(result_json_path, "r", encoding="utf-8") as fh:
                    batch_result_data = json.loads(extract_json_text(fh.read()))
            except Exception:
                logging.exception("batch result JSON parse failed for %s", job_id)

        generated_keys = set()
        batch_summaries = {
            str(item.get("key", "")): item
            for item in batch_result_data.get("items", []) or []
            if isinstance(item, dict)
        }
        for item in remaining:
            key = str(item.get("key", "")).strip()
            workspace_output_path = str(item.get("workspace_output_path", "")).strip()
            public_output_path = str(item.get("public_output_path", "")).strip()
            if not key or not os.path.isfile(workspace_output_path):
                continue
            shutil.copy2(workspace_output_path, public_output_path)
            cache_id = store_to_cache(source_url, drama_name, item)
            result_data = dict(batch_summaries.get(key) or {})
            result_data.update(
                {
                    "key": key,
                    "status": "done",
                    "workspace_output_path": workspace_output_path,
                    "public_output_path": public_output_path,
                    "public_url": build_public_url(public_output_path),
                    "generator": "codex-imagegen",
                    "cache": "miss",
                    "cache_key": cache_id,
                    "batch": True,
                }
            )
            results.append(result_data)
            generated_keys.add(key)
        remaining = [item for item in remaining if str(item.get("key", "")).strip() not in generated_keys]

    for item in remaining:
        key = str(item.get("key", "")).strip()
        workspace_output_path = str(item.get("workspace_output_path", "")).strip()
        public_output_path = str(item.get("public_output_path", "")).strip()
        if not key:
            raise ValueError("item.key is required")
        if not workspace_output_path:
            raise ValueError("workspace_output_path is required")
        if not public_output_path:
            raise ValueError("public_output_path is required")
        ensure_dir(os.path.dirname(workspace_output_path))
        ensure_dir(os.path.dirname(public_output_path))
        if os.path.exists(workspace_output_path):
            os.remove(workspace_output_path)

        result_json_path = os.path.join(workdir, "%s_result.json" % key)
        cmd = [
            CODEX_BIN,
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            ws,
            "-i",
            staged_source_path,
            "-o",
            result_json_path,
            build_codex_instruction(drama_name, item),
        ]
        run_cmd(cmd, timeout=CODEX_TIMEOUT)
        if not os.path.isfile(workspace_output_path):
            raise RuntimeError("Codex did not create expected output for %s" % key)
        shutil.copy2(workspace_output_path, public_output_path)
        with open(result_json_path, "r", encoding="utf-8") as fh:
            result_data = json.loads(extract_json_text(fh.read()))
        cache_id = store_to_cache(source_url, drama_name, item)
        result_data.update(
            {
                "key": key,
                "status": "done",
                "workspace_output_path": workspace_output_path,
                "public_output_path": public_output_path,
                "public_url": build_public_url(public_output_path),
                "generator": "codex-imagegen",
                "cache": "miss",
                "cache_key": cache_id,
            }
        )
        with open(result_json_path, "w", encoding="utf-8") as fh:
            json.dump(result_data, fh, ensure_ascii=False, indent=2)
        results.append(result_data)

    if not KEEP_JOB_WORKSPACE:
        shutil.rmtree(ws, ignore_errors=True)

    return {"status": "done", "job_id": job_id, "items": results}


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class CodexScreenshotHandler(BaseHTTPRequestHandler):
    server_version = "CodexScreenshotService/1.1"

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _read_json(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8")) if body else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/files/"):
            rel_path = unquote(parsed.path[len("/files/") :]).lstrip("/")
            abs_path = os.path.abspath(os.path.join(PUBLIC_ROOT, rel_path))
            public_root = os.path.abspath(PUBLIC_ROOT)
            if not (abs_path == public_root or abs_path.startswith(public_root + os.sep)):
                json_response(self, 403, {"error": "forbidden"})
                return
            if not os.path.isfile(abs_path):
                json_response(self, 404, {"error": "not_found"})
                return
            content_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(os.path.getsize(abs_path)))
            self.end_headers()
            with open(abs_path, "rb") as fp:
                shutil.copyfileobj(fp, self.wfile)
            return

        if parsed.path != "/healthz":
            json_response(self, 404, {"error": "not_found"})
            return
        json_response(
            self,
            200,
            {
                "status": "ok",
                "max_concurrency": MAX_CONCURRENCY,
                "public_base_url": PUBLIC_BASE_URL,
                "cache_root": CACHE_ROOT,
                "prompt_version": PROMPT_VERSION,
            },
        )

    def do_POST(self):
        if self.path != "/api/codex-screenshot/generate":
            json_response(self, 404, {"error": "not_found"})
            return
        try:
            payload = self._read_json()
            with GENERATION_SEMAPHORE:
                result = generate_screenshots(payload)
            json_response(self, 200, result)
        except Exception as exc:
            logging.exception("generate failed")
            json_response(self, 500, {"status": "error", "error": str(exc).strip() or exc.__class__.__name__})


def main():
    server = ThreadedHTTPServer((HOST, PORT), CodexScreenshotHandler)
    logging.info("listening on %s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
