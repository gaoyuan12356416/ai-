#!/usr/bin/env python3
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


HOST = os.environ.get("AD_MATERIAL_GENERATION_HOST", "127.0.0.1")
PORT = int(os.environ.get("AD_MATERIAL_GENERATION_PORT", "8797"))
WORK_ROOT = Path(os.environ.get("AD_MATERIAL_GENERATION_WORK_ROOT", "/root/ad_material_generation_jobs"))
PUBLIC_ROOT = Path(os.environ.get("AD_MATERIAL_GENERATION_PUBLIC_ROOT", "/usr/share/nginx/html/ad-material-generation"))
PUBLIC_BASE_URL = os.environ.get("AD_MATERIAL_GENERATION_PUBLIC_BASE_URL", "http://127.0.0.1:18797/files")
PROMPT_WORKSPACE = Path(os.environ.get("AD_MATERIAL_GENERATION_PROMPT_WORKSPACE", "/root/ad_material_generation_workspace"))
CODEX_BIN = os.environ.get("AD_MATERIAL_GENERATION_CODEX_BIN", "/usr/bin/codex")
CODEX_MODEL = os.environ.get("AD_MATERIAL_GENERATION_CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
CODEX_REASONING = os.environ.get("AD_MATERIAL_GENERATION_CODEX_REASONING", "medium").strip() or "medium"
CODEX_TIMEOUT = int(os.environ.get("AD_MATERIAL_GENERATION_CODEX_TIMEOUT", "2400"))
MAX_CONCURRENCY = max(1, int(os.environ.get("AD_MATERIAL_GENERATION_MAX_CONCURRENCY", "1")))
REQUEST_BODY_LIMIT = int(os.environ.get("AD_MATERIAL_GENERATION_BODY_LIMIT", str(8 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = int(os.environ.get("AD_MATERIAL_GENERATION_DOWNLOAD_TIMEOUT", "90"))
MAX_REFERENCE_IMAGES = max(0, int(os.environ.get("AD_MATERIAL_GENERATION_MAX_REFERENCE_IMAGES", "8")))

SEMAPHORE = threading.Semaphore(MAX_CONCURRENCY)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")


def ensure_dir(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def file_response(handler, path):
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(path.stat().st_size))
    handler.end_headers()
    with path.open("rb") as fp:
        shutil.copyfileobj(fp, handler.wfile)


def public_url(path):
    rel = path.resolve().relative_to(PUBLIC_ROOT.resolve()).as_posix()
    return PUBLIC_BASE_URL.rstrip("/") + "/" + rel


def safe_slug(value, fallback="job"):
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-._")
    return text[:80] or fallback


def target_dimensions(size):
    text = str(size or "").lower().replace(" ", "")
    match = re.search(r"(\d{3,4})\s*[x*]\s*(\d{3,4})", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    if "4:5" in text:
        return 1080, 1350
    if "9:16" in text:
        return 1080, 1920
    if "1.91" in text or "16:9" in text:
        return 1200, 628
    return 1080, 1080


def extract_reference_urls(text):
    urls = []
    seen = set()
    patterns = [
        r'<img[^>]+src=["\']([^"\']+)["\']',
        r'!\[[^\]]*\]\((https?://[^)\s]+)\)',
        r'(https?://[^\s"\'<>]+)',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", flags=re.I):
            url = match.group(1).strip().rstrip(").,;")
            suffix = Path(urlparse(url).path).suffix.lower()
            if suffix in IMAGE_EXTS and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def download_url(url, path):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        data = resp.read(25 * 1024 * 1024)
    if len(data) < 512:
        raise ValueError("downloaded reference too small: %s" % url)
    path.write_bytes(data)


def stage_reference_images(task, demand_text, workdir):
    urls = []
    for ref in task.get("reference_files") or []:
        if isinstance(ref, dict):
            value = str(ref.get("archive_url") or ref.get("url") or "").strip()
            if value:
                urls.append(value)
    urls.extend(extract_reference_urls(demand_text))

    staged = []
    seen = set()
    ref_dir = workdir / "references"
    ensure_dir(ref_dir)
    for url in urls:
        if len(staged) >= MAX_REFERENCE_IMAGES:
            break
        if url in seen:
            continue
        seen.add(url)
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in IMAGE_EXTS:
            suffix = ".jpg"
        out = ref_dir / ("ref_%02d%s" % (len(staged) + 1, suffix))
        try:
            download_url(url, out)
            staged.append(str(out))
        except Exception:
            logging.exception("failed to stage reference image: %s", url)
    return staged


def extract_asset_section(demand_text, index):
    text = demand_text or ""
    markers = [
        r"(?im)^#{1,4}\s*(?:素材|Material|Asset)\s*0*%d\b.*$" % index,
        r"(?im)^\s*(?:素材|Material|Asset)\s*0*%d\b.*$" % index,
    ]
    for pattern in markers:
        match = re.search(pattern, text)
        if not match:
            continue
        start = match.start()
        next_match = re.search(r"(?im)^#{1,4}\s*(?:素材|Material|Asset)\s*0*\d+\b.*$", text[match.end() :])
        end = match.end() + next_match.start() if next_match else len(text)
        return text[start:end].strip()
    return ""


def build_prompt(task, demand_text, index, output_path, width, height, reason):
    product = task.get("product_name") or task.get("app_id") or "the product"
    section = extract_asset_section(demand_text, index)
    if not section:
        section = "Use the overall requirement document and create variant #%02d." % index
    return """You are generating a static paid-social advertising image.

Use the built-in image generation capability. Generate exactly one polished static image and save the final selected image to:
{output_path}

Hard requirements:
- Product: {product}
- Market/language: {country}/{language}
- Output size: {width}x{height}px.
- Static image only. Do not create video, gif, storyboards, mock UI code, SVG placeholders, or explanations.
- Follow the specific asset requirement for material #{index:02d}.
- Preserve only transferable layout, color, hierarchy, product cues, and ad copy from references. Do not copy competitor logos, competitor brand names, watermarks, app-store badges, policy-unsafe promises, or unreadable tiny text.
- Keep all visible text intentional, legible, and in the requested language.
- After image generation, copy or save the final image file to the exact output path above.
- Reply only with compact JSON: {{"output_path":"{output_path}","summary":"..."}}

Regeneration instruction:
{reason}

Specific material requirement:
{section}

Full requirement document:
{demand_text}
""".format(
        output_path=output_path,
        product=product,
        country=task.get("country") or "",
        language=task.get("language") or "",
        width=width,
        height=height,
        index=index,
        reason=reason or "None",
        section=section[:5000],
        demand_text=(demand_text or "")[:16000],
    )


def extract_json_text(raw_text):
    text = (raw_text or "").strip()
    if not text:
        raise RuntimeError("Codex returned empty output")
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.startswith("```")).strip()
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


def run_codex(prompt, image_paths, result_path, workdir):
    cmd = [
        CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(PROMPT_WORKSPACE),
        "-m",
        CODEX_MODEL,
        "-c",
        "model_reasoning_effort=%s" % json.dumps(CODEX_REASONING),
    ]
    for image_path in image_paths:
        cmd.extend(["-i", image_path])
    cmd.extend(["-o", str(result_path), prompt])
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=CODEX_TIMEOUT,
    )
    (workdir / "codex_stdout_stderr.log").write_text((proc.stdout or "") + "\n" + (proc.stderr or ""), encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError("Codex generation failed (%s): %s" % (proc.returncode, (proc.stderr or proc.stdout or "")[-2000:]))
    if not result_path.exists():
        raise RuntimeError("Codex did not write result JSON")
    return json.loads(extract_json_text(result_path.read_text(encoding="utf-8")))


def generate(payload):
    task = payload.get("task") or {}
    task_id = safe_slug(task.get("task_id") or payload.get("task_id") or str(uuid.uuid4()))
    indexes = payload.get("indexes") or []
    if not indexes:
        indexes = list(range(1, int(task.get("quantity") or 1) + 1))
    indexes = [int(index) for index in indexes]
    reason = str(payload.get("reason") or "").strip()
    demand_text = str(task.get("demand_text") or payload.get("demand_text") or "").strip()
    if not demand_text:
        raise ValueError("demand_text is required")

    width, height = target_dimensions(task.get("size"))
    workdir = WORK_ROOT / task_id
    ensure_dir(workdir)
    ensure_dir(PUBLIC_ROOT / task_id)
    ensure_dir(PROMPT_WORKSPACE)
    reference_images = stage_reference_images(task, demand_text, workdir)

    outputs = []
    for index in indexes:
        asset_id = "%s_%02d" % (task_id, index)
        output_path = PUBLIC_ROOT / task_id / ("%s.png" % asset_id)
        result_path = workdir / ("%s_result.json" % asset_id)
        if output_path.exists():
            output_path.unlink()
        prompt = build_prompt(task, demand_text, index, str(output_path), width, height, reason)
        result = run_codex(prompt, reference_images, result_path, workdir)
        result_output = Path(str(result.get("output_path") or output_path))
        if result_output != output_path and result_output.exists():
            shutil.copy2(result_output, output_path)
        if not output_path.is_file() or output_path.stat().st_size < 1024:
            raise RuntimeError("missing generated image for asset %02d" % index)
        outputs.append(
            {
                "asset_id": asset_id,
                "asset_index": index,
                "name": "%s_%02d" % (task.get("product_name") or "ad_material", index),
                "url": public_url(output_path),
                "public_url": public_url(output_path),
                "generator": "codex-imagegen",
                "summary": str(result.get("summary") or ""),
                "width": width,
                "height": height,
            }
        )
    return {"status": "done", "task_id": task_id, "outputs": outputs}


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "AdMaterialGenerationService/1.0"

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            json_response(self, 200, {"ok": True, "service": "ad-material-generation"})
            return
        if parsed.path.startswith("/files/"):
            rel = unquote(parsed.path[len("/files/") :]).lstrip("/")
            path = (PUBLIC_ROOT / rel).resolve()
            public_root = PUBLIC_ROOT.resolve()
            if path != public_root and public_root not in path.parents:
                json_response(self, 403, {"ok": False, "error": "forbidden"})
                return
            if not path.is_file():
                json_response(self, 404, {"ok": False, "error": "not_found"})
                return
            file_response(self, path)
            return
        json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if self.path != "/api/ad-material-generation/generate":
            json_response(self, 404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > REQUEST_BODY_LIMIT:
                raise ValueError("invalid request body length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            with SEMAPHORE:
                result = generate(payload)
            json_response(self, 200, {"ok": True, **result})
        except Exception as exc:
            logging.exception("ad material generation failed")
            json_response(self, 500, {"ok": False, "error": str(exc)})


def main():
    ensure_dir(WORK_ROOT)
    ensure_dir(PUBLIC_ROOT)
    ensure_dir(PROMPT_WORKSPACE)
    server = ThreadedHTTPServer((HOST, PORT), Handler)
    logging.info("ad material generation service listening on %s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
