#!/usr/bin/env python3
import json
import logging
import mimetypes
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlparse

import requests

HOST = os.environ.get("CODEX_COVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_COVER_PORT", "8790"))
WORK_ROOT = os.environ.get("CODEX_COVER_WORK_ROOT", "/root/codex_cover_jobs")
PUBLIC_ROOT = os.environ.get("CODEX_COVER_PUBLIC_ROOT", "/usr/share/nginx/html/drama-materials")
PUBLIC_BASE_URL = os.environ.get(
    "CODEX_COVER_PUBLIC_BASE_URL", "https://ai.yingliangads.com/drama-materials"
)
PROMPT_WORKSPACE = os.environ.get(
    "CODEX_COVER_PROMPT_WORKSPACE", "/root/codex_cover_worker_workspace"
)
CODEX_BIN = os.environ.get("CODEX_COVER_CODEX_BIN", "/usr/bin/codex")
CODEX_TIMEOUT = int(os.environ.get("CODEX_COVER_CODEX_TIMEOUT", "1200"))
CODEX_COVER_STRATEGY = os.environ.get("CODEX_COVER_STRATEGY", "safe_composite").strip().lower()
MAX_CONCURRENCY = max(1, int(os.environ.get("CODEX_COVER_MAX_CONCURRENCY", "1")))
GENERATION_SEMAPHORE = __import__("threading").Semaphore(MAX_CONCURRENCY)

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
    raise RuntimeError("Codex did not return valid JSON")


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


def build_codex_instruction(drama_name, workspace_output_path):
    title = drama_name.strip() or "the provided drama"
    return (
        "Use the built-in image generation or image editing capability to turn the attached vertical drama poster into a premium cinematic horizontal 16:9 cover. "
        "Preserve the same identities, facial features, costumes, color mood, and original visible title text in its original language. "
        "Do not translate, localize, or replace the title text. "
        "Do not add extra people, duplicate limbs, watermarks, logos, collage seams, or any new text. "
        "Make the final image feel like a polished short-drama key art cover for %s. "
        "After generation, copy the selected final image into %s. "
        "Reply with only compact JSON containing output_path and summary."
        % (title, workspace_output_path)
    )


def parse_size(value, default=(1920, 1080)):
    text = str(value or "").lower().replace("*", "x")
    if "x" not in text:
        return default
    left, right = text.split("x", 1)
    try:
        width = int(left.strip())
        height = int(right.strip())
        if width > 0 and height > 0:
            return width, height
    except ValueError:
        pass
    return default


def compose_safe_cover(source_path, output_path):
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except Exception as exc:
        raise RuntimeError("Pillow is required for safe cover composition: %s" % exc)

    target_size = parse_size(os.environ.get("CODEX_COVER_OUTPUT_SIZE"), (1920, 1080))
    with Image.open(source_path) as raw:
        source = raw.convert("RGB")
    resample_lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)

    background = ImageOps.fit(
        source,
        target_size,
        method=resample_lanczos,
        centering=(0.5, 0.45),
    )
    background = background.filter(ImageFilter.GaussianBlur(radius=34))
    background = ImageEnhance.Brightness(background).enhance(0.68)
    background = ImageEnhance.Color(background).enhance(0.88)

    foreground = ImageOps.contain(
        source,
        (int(target_size[0] * 0.72), target_size[1]),
        method=resample_lanczos,
    )
    x = (target_size[0] - foreground.width) // 2
    y = (target_size[1] - foreground.height) // 2

    shadow = Image.new("RGBA", target_size, (0, 0, 0, 0))
    shadow_box = Image.new("RGBA", foreground.size, (0, 0, 0, 150))
    shadow.paste(shadow_box, (x + 18, y + 18), shadow_box)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=28))

    canvas = background.convert("RGBA")
    canvas.alpha_composite(shadow)
    mask = Image.new("L", foreground.size, 255)
    fade = min(56, max(1, foreground.width // 12))
    for i in range(fade):
        alpha = int(255 * (i + 1) / fade)
        mask.paste(alpha, (i, 0, i + 1, foreground.height))
        mask.paste(alpha, (foreground.width - i - 1, 0, foreground.width - i, foreground.height))
    canvas.paste(foreground, (x, y), mask)
    canvas.convert("RGB").save(output_path, "JPEG", quality=92, optimize=True)


def generate_cover(payload):
    job_id = str(payload.get("job_id", "")).strip()
    source_path = str(payload.get("source_path", "")).strip()
    source_url = str(payload.get("source_url", "")).strip()
    drama_name = str(payload.get("drama_name", "")).strip()
    workspace_output_path = str(payload.get("workspace_output_path", "")).strip()
    public_output_path = str(payload.get("public_output_path", "")).strip()
    if not job_id:
        raise ValueError("job_id is required")
    if not workspace_output_path:
        raise ValueError("workspace_output_path is required")
    if not public_output_path:
        raise ValueError("public_output_path is required")

    workdir = os.path.join(WORK_ROOT, job_id)
    ensure_dir(workdir)
    ensure_dir(PROMPT_WORKSPACE)
    ensure_dir(os.path.dirname(workspace_output_path))
    ensure_dir(os.path.dirname(public_output_path))

    staged_source_path = prepare_source_file(source_path, source_url, workdir)
    result_json_path = os.path.join(workdir, "codex_result.json")
    if os.path.exists(workspace_output_path):
        os.remove(workspace_output_path)
    if CODEX_COVER_STRATEGY in ("safe_composite", "composite", "deterministic"):
        compose_safe_cover(staged_source_path, workspace_output_path)
        shutil.copy2(workspace_output_path, public_output_path)
        result_data = {
            "status": "done",
            "job_id": job_id,
            "workspace_output_path": workspace_output_path,
            "public_output_path": public_output_path,
            "public_url": build_public_url(public_output_path),
            "generator": "safe-composite",
            "summary": "Built a deterministic 16:9 cover from the source poster without AI outpainting.",
        }
        with open(result_json_path, "w", encoding="utf-8") as fh:
            json.dump(result_data, fh, ensure_ascii=False, indent=2)
        return result_data
    cmd = [
        CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        PROMPT_WORKSPACE,
        "-i",
        staged_source_path,
        "-o",
        result_json_path,
        build_codex_instruction(drama_name, workspace_output_path),
    ]
    run_cmd(cmd, timeout=CODEX_TIMEOUT)
    if not os.path.isfile(workspace_output_path):
        raise RuntimeError("Codex did not create output")
    shutil.copy2(workspace_output_path, public_output_path)
    with open(result_json_path, "r", encoding="utf-8") as fh:
        result_data = json.loads(extract_json_text(fh.read()))
    result_data.update(
        {
            "status": "done",
            "job_id": job_id,
            "workspace_output_path": workspace_output_path,
            "public_output_path": public_output_path,
            "public_url": build_public_url(public_output_path),
            "generator": "codex-imagegen",
        }
    )
    with open(result_json_path, "w", encoding="utf-8") as fh:
        json.dump(result_data, fh, ensure_ascii=False, indent=2)
    return result_data


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class CodexCoverHandler(BaseHTTPRequestHandler):
    server_version = "CodexCoverService/1.0"

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _read_json(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        body = self.rfile.read(content_length)
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

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
            },
        )

    def do_POST(self):
        if self.path != "/api/codex-cover/generate":
            json_response(self, 404, {"error": "not_found"})
            return
        try:
            payload = self._read_json()
            with GENERATION_SEMAPHORE:
                result = generate_cover(payload)
            json_response(self, 200, result)
        except Exception as exc:
            logging.exception("cover generation failed")
            json_response(self, 500, {"error": str(exc)})


def main():
    ensure_dir(WORK_ROOT)
    ensure_dir(PUBLIC_ROOT)
    ensure_dir(PROMPT_WORKSPACE)
    server = ThreadedHTTPServer((HOST, PORT), CodexCoverHandler)
    logging.info("codex cover service listening on %s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
