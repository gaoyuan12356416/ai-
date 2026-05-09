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
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
RESAMPLE_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")

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
ISOLATE_CODEX_HOME = os.environ.get("CODEX_SCREENSHOT_ISOLATE_CODEX_HOME", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
SOURCE_CODEX_HOME = os.environ.get("CODEX_SCREENSHOT_SOURCE_CODEX_HOME", "/root/.codex")
CODEX_MODEL = os.environ.get("CODEX_SCREENSHOT_CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
CODEX_REASONING = os.environ.get("CODEX_SCREENSHOT_CODEX_REASONING", "medium").strip() or "medium"
ASPECT_RATIO_TOLERANCE = float(os.environ.get("CODEX_SCREENSHOT_ASPECT_RATIO_TOLERANCE", "0.03"))

# Cache: avoids repeated generations for same source + spec + prompt version.
CACHE_ROOT = os.environ.get("CODEX_SCREENSHOT_CACHE_ROOT", "/root/codex_screenshot_cache")
PROMPT_VERSION = os.environ.get("CODEX_SCREENSHOT_PROMPT_VERSION", "v3")
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


def run_cmd(cmd, timeout=None, env=None):
    logging.info("running: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "command failed (%s): %s"
            % (proc.returncode, proc.stderr.strip() or proc.stdout.strip())
        )
    return proc


def toml_string(value):
    return json.dumps(str(value))


def prepare_isolated_codex_home(workdir, project_dir):
    codex_home = os.path.join(workdir, "codex_home")
    if os.path.isdir(codex_home):
        shutil.rmtree(codex_home, ignore_errors=True)
    elif os.path.exists(codex_home):
        os.remove(codex_home)
    ensure_dir(codex_home)
    for dirname in ("generated_images", "sessions", "log", "tmp", "shell_snapshots"):
        ensure_dir(os.path.join(codex_home, dirname))

    # Keep this path as a plain file so Codex cannot auto-install bundled
    # system skills such as imagegen into the subprocess context.
    with open(os.path.join(codex_home, "skills"), "w", encoding="utf-8") as fh:
        fh.write("system skills disabled for screenshot generation\n")

    for filename in (
        "auth.json",
        "installation_id",
        "version.json",
        "models_cache.json",
        ".personality_migration",
    ):
        source = os.path.join(SOURCE_CODEX_HOME, filename)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(codex_home, filename))

    config = "\n".join(
        [
            "model = %s" % toml_string(CODEX_MODEL),
            "model_reasoning_effort = %s" % toml_string(CODEX_REASONING),
            'approval_policy = "never"',
            "",
            "[projects.%s]" % toml_string(project_dir),
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


def build_codex_env(workdir, project_dir):
    if not ISOLATE_CODEX_HOME:
        return None
    env = os.environ.copy()
    env["CODEX_HOME"] = prepare_isolated_codex_home(workdir, project_dir)
    return env


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
    try:
        if image_dimensions(cached_img) != target_dimensions(item):
            logging.warning("skip screenshot cache with bad dimensions: %s", cached_img)
            return None
    except Exception:
        logging.warning("skip unreadable screenshot cache: %s", cached_img)
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


def target_dimensions(item):
    return int(item.get("width") or 0), int(item.get("height") or 0)


def target_aspect_ratio(item):
    width, height = target_dimensions(item)
    if width <= 0 or height <= 0:
        raise ValueError("invalid target dimensions for %s" % str(item.get("key", "")).strip())
    return width / float(height)


def image_dimensions(path):
    with Image.open(path) as image:
        return image.size


def image_aspect_ratio(path):
    width, height = image_dimensions(path)
    if height <= 0:
        raise RuntimeError("invalid image height: %s" % path)
    return width / float(height), width, height


def aspect_ratio_error(actual, target):
    if target <= 0:
        return 1.0
    return abs(actual - target) / target


def validate_raw_generated_image(item, result_data):
    key = str(item.get("key", "")).strip()
    raw_path = str((result_data or {}).get("raw_generated_path") or "").strip()
    if not raw_path:
        raise RuntimeError("missing raw_generated_path for %s" % key)
    if not os.path.isfile(raw_path):
        raise RuntimeError("raw_generated_path not found for %s: %s" % (key, raw_path))

    raw_ratio, raw_width, raw_height = image_aspect_ratio(raw_path)
    target_ratio = target_aspect_ratio(item)
    error = aspect_ratio_error(raw_ratio, target_ratio)
    if error > ASPECT_RATIO_TOLERANCE:
        raise RuntimeError(
            "raw aspect ratio rejected for %s: %sx%s ratio %.6f, target %.6f, error %.2f%% > %.2f%%"
            % (
                key,
                raw_width,
                raw_height,
                raw_ratio,
                target_ratio,
                error * 100.0,
                ASPECT_RATIO_TOLERANCE * 100.0,
            )
        )
    data = dict(result_data or {})
    data.update(
        {
            "raw_generated_path": raw_path,
            "raw_width": raw_width,
            "raw_height": raw_height,
            "raw_ratio": round(raw_ratio, 10),
            "target_ratio": round(target_ratio, 10),
            "aspect_ratio_error": round(error, 10),
            "aspect_ratio_tolerance": ASPECT_RATIO_TOLERANCE,
            "aspect_ratio_valid": True,
        }
    )
    return data


def normalize_without_crop(item, source_path, output_path):
    width, height = target_dimensions(item)
    ensure_dir(os.path.dirname(output_path))
    with Image.open(source_path) as image:
        image = image.convert("RGB")
        if image.size != (width, height):
            image = image.resize((width, height), RESAMPLE_LANCZOS)
        image.save(output_path, "JPEG", quality=94, optimize=True, progressive=True)


def validate_and_normalize_generated_output(item, result_data, workspace_output_path):
    result_data = validate_raw_generated_image(item, result_data)
    raw_path = result_data["raw_generated_path"]
    normalize_without_crop(item, raw_path, workspace_output_path)
    final_width, final_height = image_dimensions(workspace_output_path)
    expected = target_dimensions(item)
    if (final_width, final_height) != expected:
        raise RuntimeError(
            "bad normalized size for %s: %sx%s expected %sx%s"
            % (
                str(item.get("key", "")).strip(),
                final_width,
                final_height,
                expected[0],
                expected[1],
            )
        )
    result_data.update(
        {
            "normalized_width": final_width,
            "normalized_height": final_height,
            "normalized_method": "resize_without_crop",
        }
    )
    return result_data


def job_workspace(job_id):
    # Each job runs in a clean workspace to avoid stale files inflating token usage.
    return os.path.join(PROMPT_WORKSPACE_ROOT, "_jobs", job_id)


def reset_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    ensure_dir(path)


def build_codex_instruction(drama_name, item):
    title = (drama_name or "").strip() or "the provided drama"
    target_ratio = target_aspect_ratio(item)
    tolerance_percent = ASPECT_RATIO_TOLERANCE * 100.0
    return (
        "Use the attached original drama cover as the source image. "
        "Create a new finished paid-social key art image for {title} at exactly {width}x{height} pixels, aspect ratio {ratio}. "
        "The raw AI-generated image itself must naturally match this canvas: width={width}, height={height}, width/height={target_ratio:.6f}. "
        "After the AI generation call, inspect the raw generated image dimensions with Python/PIL. "
        "If raw width/height differs from {target_ratio:.6f} by more than {tolerance_percent:.2f}% relative error, treat that generation as unusable and retry only this target. "
        "Do at most three AI generation attempts for this target. "
        "Do not use crop, pad, blur-background, or layout conversion to hide an invalid raw aspect ratio. "
        "This must be an AI image generation or image-editing result for this exact canvas, not a deterministic crop, resize, pad, or copy-paste layout. "
        "Keep the main characters, faces, costumes, title text, logos, and visual identity recognizable from the original source, while adapting the composition naturally to the target canvas. "
        "Extend or recreate the surrounding background as needed so the result looks complete, polished, and not like a cropped or stretched image. "
        "Do not add watermarks, unrelated props, extra people, duplicate limbs, deformed hands, or collage seams. "
        "After a target-ratio raw AI image exists, copy or JPEG-convert the selected raw image into {output_path}; do not crop, pad, or stretch it. "
        "Reply with only compact JSON containing output_path, raw_generated_path, raw_width, raw_height, raw_ratio, used_ai_generation=true, retry_count, and summary."
    ).format(
        ratio=item["ratio"],
        width=int(item["width"]),
        height=int(item["height"]),
        target_ratio=target_ratio,
        tolerance_percent=tolerance_percent,
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
        "Each requested canvas must be an independently composed AI image generation or image-editing result, not crops, resizes, pads, or copy-paste derivatives from one generated image. "
        "Keep the main characters, faces, costumes, title text, logos, and visual identity recognizable from the original source across all outputs. "
        "Adapt each composition naturally to its target canvas, extending or recreating the surrounding background as needed so each image looks complete, polished, and not cropped or stretched. "
        "Do not add watermarks, unrelated props, extra people, duplicate limbs, deformed hands, or collage seams. "
        "Requested outputs: {specs}. "
        "After generation, copy each selected final image into its exact output_path. "
        "Reply with only compact JSON containing an items array with key, output_path, and summary."
    ).format(title=title, specs=json.dumps(specs, ensure_ascii=False, separators=(",", ":")))


def build_codex_batch_imagegen_instruction(drama_name, items, manifest_path=None):
    title = (drama_name or "").strip() or "the provided drama"
    specs = []
    plan_steps = []
    for item in items:
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        spec = {
            "key": str(item.get("key", "")).strip(),
            "label": str(item.get("label") or item.get("key") or "").strip(),
            "ratio": str(item.get("ratio", "")).strip(),
            "width": width,
            "height": height,
            "target_width_height_ratio": round(width / float(height), 10) if height else 0,
            "max_relative_ratio_error": ASPECT_RATIO_TOLERANCE,
            "output_path": str(item.get("workspace_output_path", "")).strip(),
        }
        specs.append(spec)
        plan_steps.append(
            "CALL_{index}: TARGET_KEY={key}; CANVAS={width}x{height}; RATIO={ratio}; OUTPUT={output_path}".format(
                index=len(plan_steps) + 1,
                key=spec["key"],
                width=spec["width"],
                height=spec["height"],
                ratio=spec["ratio"],
                output_path=spec["output_path"],
            )
        )
    manifest = (manifest_path or "").strip()
    manifest_rule = (
        "Also write the same JSON object to {manifest_path}. ".format(manifest_path=manifest)
        if manifest
        else ""
    )
    return (
        "This is an isolated batch-image-generation test. "
        "Use the attached original drama cover only as the visual identity reference. "
        "In this single Codex subprocess, produce exactly one selected AI-generated paid-social key art image for each target below, in this exact order: {plan}. "
        "Do not inspect memories, Git history, web pages, logs, databases, unrelated project files, or any skill files. "
        "Do not read or use any local skill file, local helper script, plugin helper, external image-generation CLI, or web service for image generation. "
        "Hard rules: use only direct built-in AI image generation or image-editing calls for the creative artwork; "
        "do not create a warmup image, sample image, exploratory variant, optional alternate candidate, or quality-only retry; "
        "do not generate a second image for a target key once that key has produced a usable image; "
        "a usable image must have a raw AI-generated width/height ratio within {tolerance_percent:.2f}% relative error of that target's target_width_height_ratio; "
        "retry only if the target-specific AI call fails technically with no usable image file or fails this raw aspect-ratio check, and record any retry in the summary; "
        "do at most three AI generation attempts per target key; "
        "do not create the creative artwork with Python, PIL, OpenCV, ImageMagick, ffmpeg, HTML, CSS, or deterministic image processing; "
        "do not crop, resize, pad, blur-background, or copy-paste the source image as a substitute for AI generation; "
        "do not use a generated image for one ratio as the source for another ratio; "
        "Python/PIL may be used only after each target-specific AI image exists, first to inspect raw dimensions, then only to JPEG-convert or resize a raw image that already passed the target-ratio check. "
        "Do not use PIL crop, ImageOps.fit, pad, stretch, blurred background, or any geometry-changing layout trick to make a bad-ratio raw image pass. "
        "Keep the main characters, faces, costumes, title text, logos, and visual identity recognizable from the source, while adapting each composition naturally to its target canvas. "
        "Each output should look like polished OTT short-drama advertising key art, not a layout conversion. "
        "Requested outputs: {specs}. "
        "For each requested output, save the final image to its exact output_path after the raw image passes aspect-ratio validation. "
        "Return compact JSON only, with an items array. Each item must include key, output_path, raw_generated_path, raw_width, raw_height, raw_ratio, used_ai_generation=true, retry_count, and summary. "
        "{manifest_rule}"
        "If you cannot produce all three target-specific AI-generated outputs inside this one subprocess, fail explicitly instead of fabricating outputs."
    ).format(
        title=title,
        plan=" | ".join(plan_steps),
        specs=json.dumps(specs, ensure_ascii=False, separators=(",", ":")),
        tolerance_percent=ASPECT_RATIO_TOLERANCE * 100.0,
        manifest_rule=manifest_rule,
    )


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
        manifest_json_path = os.path.join(workdir, "batch_manifest.json")
        codex_env = build_codex_env(workdir, ws)
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
            build_codex_batch_imagegen_instruction(drama_name, remaining, manifest_json_path),
        ]
        try:
            run_cmd(cmd, timeout=CODEX_TIMEOUT, env=codex_env)
        except Exception:
            logging.exception("batch generation failed for %s", job_id)

        batch_result_data = {"items": []}
        batch_result_source = result_json_path if os.path.isfile(result_json_path) else manifest_json_path
        if os.path.isfile(batch_result_source):
            try:
                with open(batch_result_source, "r", encoding="utf-8") as fh:
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
            result_data = dict(batch_summaries.get(key) or {})
            try:
                result_data = validate_and_normalize_generated_output(
                    item, result_data, workspace_output_path
                )
            except Exception as exc:
                logging.warning(
                    "batch generated output rejected: job=%s key=%s error=%s",
                    job_id,
                    key,
                    str(exc).strip() or exc.__class__.__name__,
                )
                try:
                    os.remove(workspace_output_path)
                except OSError:
                    pass
                try:
                    if public_output_path and os.path.exists(public_output_path):
                        os.remove(public_output_path)
                except OSError:
                    pass
                continue
            shutil.copy2(workspace_output_path, public_output_path)
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
        codex_env = build_codex_env(workdir, ws)
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
        run_cmd(cmd, timeout=CODEX_TIMEOUT, env=codex_env)
        if not os.path.isfile(workspace_output_path):
            raise RuntimeError("Codex did not create expected output for %s" % key)
        with open(result_json_path, "r", encoding="utf-8") as fh:
            result_data = json.loads(extract_json_text(fh.read()))
        result_data = validate_and_normalize_generated_output(
            item, result_data, workspace_output_path
        )
        shutil.copy2(workspace_output_path, public_output_path)
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
