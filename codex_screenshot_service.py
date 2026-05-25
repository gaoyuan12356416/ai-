#!/usr/bin/env python3
import concurrent.futures
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlparse

import requests
from PIL import Image, ImageFile, ImageOps

ImageFile.LOAD_TRUNCATED_IMAGES = True
RESAMPLE_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def positive_int_env(name, default):
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(1, int(default))


HOST = os.environ.get("CODEX_SCREENSHOT_HOST", "127.0.0.1")
PORT = positive_int_env("CODEX_SCREENSHOT_PORT", 8791)
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
CODEX_TIMEOUT = positive_int_env("CODEX_SCREENSHOT_CODEX_TIMEOUT", 1800)
CONSISTENCY_TIMEOUT = positive_int_env("CODEX_SCREENSHOT_CONSISTENCY_TIMEOUT", 300)
MAX_CONCURRENCY = positive_int_env("CODEX_SCREENSHOT_MAX_CONCURRENCY", 1)
GENERATION_SEMAPHORE = threading.Semaphore(MAX_CONCURRENCY)
ITEM_PARALLELISM = positive_int_env("CODEX_SCREENSHOT_ITEM_PARALLELISM", 1)
AI_ATTEMPTS = positive_int_env("CODEX_SCREENSHOT_AI_ATTEMPTS", 2)
CODEX_EXTRA_ARGS = shlex.split(os.environ.get("CODEX_SCREENSHOT_CODEX_EXTRA_ARGS", ""))
ISOLATE_CODEX_HOME = os.environ.get("CODEX_SCREENSHOT_ISOLATE_CODEX_HOME", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
SOURCE_CODEX_HOME = os.environ.get("CODEX_SCREENSHOT_SOURCE_CODEX_HOME", "/root/.codex")
AUTH_SYNC_LOCK_PATH = os.environ.get("CODEX_SCREENSHOT_AUTH_SYNC_LOCK_PATH", "/tmp/codex_screenshot_auth_sync.lock")
SYNC_ISOLATED_AUTH = os.environ.get("CODEX_SCREENSHOT_SYNC_ISOLATED_AUTH", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
CODEX_MODEL = os.environ.get("CODEX_SCREENSHOT_CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
CODEX_REASONING = os.environ.get("CODEX_SCREENSHOT_CODEX_REASONING", "medium").strip() or "medium"
ASPECT_RATIO_TOLERANCE = float(os.environ.get("CODEX_SCREENSHOT_ASPECT_RATIO_TOLERANCE", "0.08"))
SOURCE_CONSISTENCY_STRICT = os.environ.get("CODEX_SCREENSHOT_SOURCE_CONSISTENCY_STRICT", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
SOURCE_CONSISTENCY_CRITICAL_CHECKS = {
    item.strip()
    for item in os.environ.get(
        "CODEX_SCREENSHOT_SOURCE_CONSISTENCY_CRITICAL_CHECKS",
        "main_character_count,character_identity_faces,title_text_content",
    ).split(",")
    if item.strip()
}

# Cache: avoids repeated generations for same source + spec + prompt version.
CACHE_ROOT = os.environ.get("CODEX_SCREENSHOT_CACHE_ROOT", "/root/codex_screenshot_cache")
PROMPT_VERSION = os.environ.get("CODEX_SCREENSHOT_PROMPT_VERSION", "v6")
SOURCE_CONSISTENCY_CHECK = os.environ.get("CODEX_SCREENSHOT_SOURCE_CONSISTENCY_CHECK", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
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
        error = RuntimeError(
            "command failed (%s): %s"
            % (proc.returncode, proc.stderr.strip() or proc.stdout.strip())
        )
        error.proc = proc
        raise error
    return proc


TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def nonnegative_int(value):
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def merge_token_usage(target, usage):
    if not isinstance(usage, dict):
        return target
    if target is None:
        target = {}
    for field in TOKEN_USAGE_FIELDS:
        target[field] = nonnegative_int(target.get(field)) + nonnegative_int(usage.get(field))
    target["session_count"] = nonnegative_int(target.get("session_count")) + nonnegative_int(usage.get("session_count"))
    return target


def normalize_token_usage(value):
    if not isinstance(value, dict):
        return {}
    usage = {field: nonnegative_int(value.get(field)) for field in TOKEN_USAGE_FIELDS}
    if not usage.get("total_tokens"):
        usage["total_tokens"] = nonnegative_int(value.get("token_total") or value.get("total"))
    return usage if any(usage.values()) else {}


def token_usage_from_text(text):
    import re

    text = str(text or "")
    match = re.search(r"tokens?\s+used\s*[:=]?\s*([0-9][0-9,]*)", text, re.IGNORECASE)
    if not match:
        match = re.search(r"total[_\s-]*tokens?\s*[:=]\s*([0-9][0-9,]*)", text, re.IGNORECASE)
    if not match:
        return {}
    return {"total_tokens": nonnegative_int(match.group(1).replace(",", ""))}


def token_usage_from_session_file(path):
    final_usage = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("type") != "event_msg":
                    continue
                payload = event.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                usage = normalize_token_usage(info.get("total_token_usage") or {})
                if usage:
                    final_usage = usage
                    continue
                usage = normalize_token_usage(info.get("last_token_usage") or {})
                if usage:
                    merge_token_usage(final_usage, usage)
    except Exception:
        logging.exception("failed to parse Codex token usage session: %s", path)
    if final_usage:
        final_usage["session_count"] = 1
    return final_usage


def collect_codex_token_usage(codex_home, since_epoch, proc=None):
    usage = {}
    sessions_root = os.path.join(codex_home, "sessions") if codex_home else ""
    if sessions_root and os.path.isdir(sessions_root):
        for root, _, files in os.walk(sessions_root):
            for filename in files:
                if not filename.endswith(".jsonl"):
                    continue
                path = os.path.join(root, filename)
                try:
                    if os.path.getmtime(path) < since_epoch - 5:
                        continue
                except OSError:
                    continue
                merge_token_usage(usage, token_usage_from_session_file(path))
    if not usage.get("total_tokens") and proc is not None:
        merge_token_usage(usage, token_usage_from_text((proc.stdout or "") + "\n" + (proc.stderr or "")))
    return usage if usage.get("total_tokens") else {}


def run_codex_cmd(cmd, timeout=None, env=None):
    started_at = time.time()
    proc = None
    try:
        if should_sync_isolated_auth(env):
            codex_home = env.get("CODEX_HOME")
            with isolated_auth_sync_lock():
                refresh_isolated_auth_from_source(codex_home)
        proc = run_cmd(cmd, timeout=timeout, env=env)
        if should_sync_isolated_auth(env):
            with isolated_auth_sync_lock():
                sync_isolated_auth_to_source(env.get("CODEX_HOME"))
        return proc
    except Exception as exc:
        proc = getattr(exc, "proc", None)
        if should_sync_isolated_auth(env):
            try:
                with isolated_auth_sync_lock():
                    sync_isolated_auth_to_source(env.get("CODEX_HOME"))
            except Exception:
                logging.exception("failed to sync isolated Codex auth after command failure")
        raise
    finally:
        codex_home = (env or os.environ).get("CODEX_HOME") or os.path.expanduser("~/.codex")
        token_usage = collect_codex_token_usage(codex_home, started_at, proc)
        if proc is not None:
            proc.codex_token_usage = token_usage


def should_sync_isolated_auth(env):
    return bool(
        ISOLATE_CODEX_HOME
        and SYNC_ISOLATED_AUTH
        and env
        and str(env.get("CODEX_HOME") or "").strip()
    )


class isolated_auth_sync_lock:
    def __enter__(self):
        ensure_dir(os.path.dirname(AUTH_SYNC_LOCK_PATH) or ".")
        self.fh = open(AUTH_SYNC_LOCK_PATH, "a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
            self._fcntl = fcntl
        except Exception:
            self._fcntl = None
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fcntl is not None:
                self._fcntl.flock(self.fh.fileno(), self._fcntl.LOCK_UN)
        finally:
            self.fh.close()


def copy_codex_state_file(source_dir, target_dir, filename):
    source = os.path.join(source_dir, filename)
    if not os.path.isfile(source):
        return False
    ensure_dir(target_dir)
    target = os.path.join(target_dir, filename)
    tmp = "%s.tmp.%s" % (target, os.getpid())
    shutil.copy2(source, tmp)
    os.replace(tmp, target)
    return True


def refresh_isolated_auth_from_source(codex_home):
    for filename in ("auth.json", "installation_id", "version.json", "models_cache.json", ".personality_migration"):
        copy_codex_state_file(SOURCE_CODEX_HOME, codex_home, filename)


def sync_isolated_auth_to_source(codex_home):
    if not codex_home:
        return
    try:
        copy_codex_state_file(codex_home, SOURCE_CODEX_HOME, "auth.json")
    except Exception:
        logging.exception("failed to sync isolated Codex auth back to source home")


def toml_string(value):
    return json.dumps(str(value))


def safe_path_id(value):
    text = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text)[:80]


def prepare_isolated_codex_home(workdir, project_dir, home_id=""):
    codex_home = os.path.join(workdir, "codex_home")
    home_id = safe_path_id(home_id)
    if home_id:
        codex_home = os.path.join(codex_home, home_id)
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


def build_codex_env(workdir, project_dir, home_id=""):
    if not ISOLATE_CODEX_HOME:
        return None
    env = os.environ.copy()
    env["CODEX_HOME"] = prepare_isolated_codex_home(workdir, project_dir, home_id)
    return env


def build_codex_exec_cmd(workdir, image_paths, result_json_path, instruction):
    cmd = [
        CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        CODEX_MODEL,
        "-c",
        "model_reasoning_effort=%s" % toml_string(CODEX_REASONING),
    ]
    cmd.extend(CODEX_EXTRA_ARGS)
    if isinstance(image_paths, (list, tuple)):
        images = [str(path) for path in image_paths if str(path or "").strip()]
    else:
        images = [str(image_paths)] if str(image_paths or "").strip() else []
    image_args = []
    for image_path in images:
        image_args.extend(["-i", image_path])
    cmd.extend(
        [
            "-C",
            workdir,
            *image_args,
            "-o",
            result_json_path,
            instruction,
        ]
    )
    return cmd


def build_source_consistency_instruction(item):
    key = str(item.get("key", "")).strip()
    ratio = str(item.get("ratio", "")).strip()
    width, height = target_dimensions(item)
    return (
        "Validation only. Do not generate or edit images. "
        "There are two attached images: IMAGE_1 is the original source cover, IMAGE_2 is the generated candidate for key={key}, canvas={width}x{height}, ratio={ratio}. "
        "Compare IMAGE_2 against IMAGE_1 for source-element consistency. Pass if the candidate preserves the original's core story and branding information. "
        "Required checks: same main character count; same character identities/faces; broadly same body type, posture, pose, pregnancy/belly state, embrace/hand placement, and visible body proportions; "
        "same costumes/clothing colors and major accessories; same key props, creatures, animals, weapons, wings, crown, jewelry, background symbols, and foreground objects; "
        "same title text content; close title typography style, color, hierarchy, and decorative treatment; "
        "same logo, badge, app icon, play icon, exclusive/paid badge, platform mark, and other icon families. "
        "Reject only if a core person, title text, logo/badge family, foreground prop, creature/object, or story-significant element is redesigned, removed, added, or made unrecognizable. "
        "Allow canvas-ratio adaptation, background extension, moderate title repositioning, and spacing changes that keep the original story elements recognizable. "
        "Return compact JSON only: {{\"passed\":true|false,\"reason\":\"...\",\"checks\":{{\"main_character_count\":true|false,\"character_identity_faces\":true|false,\"body_pose_body_type\":true|false,\"costumes_accessories\":true|false,\"key_props_creatures_objects\":true|false,\"title_text_content\":true|false,\"title_font_color_style\":true|false,\"logos_badges_icons_style\":true|false,\"composition_story_elements\":true|false,\"no_new_or_missing_elements\":true|false}},\"differences\":[\"...\"]}}."
    ).format(key=key, width=width, height=height, ratio=ratio)


def run_source_consistency_validation(item, source_path, generated_path, workdir, result_json_path, env=None):
    if not SOURCE_CONSISTENCY_CHECK:
        return {}, {}
    if not source_path or not os.path.isfile(source_path):
        raise RuntimeError("source consistency validation missing source image")
    if not generated_path or not os.path.isfile(generated_path):
        raise RuntimeError("source consistency validation missing generated image")
    cmd = build_codex_exec_cmd(
        workdir,
        [source_path, generated_path],
        result_json_path,
        build_source_consistency_instruction(item),
    )
    proc = run_codex_cmd(cmd, timeout=CONSISTENCY_TIMEOUT, env=env)
    with open(result_json_path, "r", encoding="utf-8") as fh:
        validation = json.loads(extract_json_text(fh.read()))
    return validation, getattr(proc, "codex_token_usage", {})


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


def normalize_source_to_jpeg(input_path, output_path):
    ensure_dir(os.path.dirname(output_path))
    with Image.open(input_path) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and image.info.get("transparency") is not None):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        image.save(output_path, "JPEG", quality=95, optimize=True, progressive=True)
    return output_path


def source_url_candidates(source_url):
    text = str(source_url or "").strip()
    if not text:
        return []
    candidates = [text]
    prefixes = (
        "https://static.mydramawave.com/",
        "http://static.mydramawave.com/",
        "https://static-v1.mydramawave.com/",
        "http://static-v1.mydramawave.com/",
        "https://static-v2.mydramawave.com/",
        "http://static-v2.mydramawave.com/",
    )
    suffix = None
    for prefix in prefixes:
        if text.startswith(prefix):
            suffix = text[len(prefix) :]
            break
    if suffix:
        for host in (
            "https://static-v1.mydramawave.com/",
            "https://static-v2.mydramawave.com/",
            "https://static.mydramawave.com/",
        ):
            candidates.append(host + suffix)
    deduped = []
    seen = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def download_and_normalize_source_url(source_url, staged_path, workdir):
    errors = []
    for index, candidate in enumerate(source_url_candidates(source_url)):
        downloaded_path = os.path.join(workdir, "source_download" if index == 0 else "source_download_%d" % index)
        try:
            response = requests.get(candidate, stream=True, timeout=180)
            response.raise_for_status()
            with open(downloaded_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
            return normalize_source_to_jpeg(downloaded_path, staged_path)
        except Exception as exc:
            message = "%s: %s" % (candidate, str(exc).strip() or exc.__class__.__name__)
            errors.append(message)
            logging.warning("failed to prepare source image candidate: %s", message)
    raise RuntimeError("source image unavailable; " + "; ".join(errors[-3:]))


def prepare_source_file(source_path, source_url, workdir):
    staged_path = os.path.join(workdir, "source.jpg")
    if source_path and os.path.isfile(source_path):
        try:
            return normalize_source_to_jpeg(source_path, staged_path)
        except Exception as exc:
            logging.warning(
                "local source image is not usable, falling back to source_url: path=%s error=%s",
                source_path,
                str(exc).strip() or exc.__class__.__name__,
            )
    if not source_url:
        raise ValueError("source_path or source_url is required")
    return download_and_normalize_source_url(source_url, staged_path, workdir)


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
        "generator": "codex-ai-image",
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


def latest_generated_image(codex_home):
    root = os.path.join(codex_home or "", "generated_images")
    if not os.path.isdir(root):
        return ""
    candidates = []
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            path = os.path.join(dirpath, filename)
            try:
                candidates.append((os.path.getmtime(path), path))
            except OSError:
                continue
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def resolve_raw_generated_path(result_data, codex_home):
    raw_path = str((result_data or {}).get("raw_generated_path") or "").strip()
    if raw_path and os.path.isfile(raw_path):
        return raw_path
    fallback = latest_generated_image(codex_home)
    return fallback or raw_path


def summarize_missing_raw_path_reason(result_data):
    parts = []
    data = result_data or {}
    consistency = data.get("source_consistency")
    if isinstance(consistency, dict):
        reason = str(consistency.get("reason") or "").strip()
        if reason:
            parts.append(reason)
        differences = consistency.get("differences")
        if isinstance(differences, list):
            parts.extend(str(item).strip() for item in differences if str(item).strip())
    summary = str(data.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    unique = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return "; ".join(unique)[:500]


def validate_raw_generated_image(item, result_data, codex_home=None):
    key = str(item.get("key", "")).strip()
    raw_path = resolve_raw_generated_path(result_data, codex_home)
    if not raw_path:
        reason = summarize_missing_raw_path_reason(result_data)
        if reason:
            raise RuntimeError(
                "ai image generation produced no output for %s: %s" % (key, reason)
            )
        raise RuntimeError("ai image generation produced no output for %s" % key)
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


def bool_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "pass", "passed")
    return False


def validate_source_consistency_report(item, result_data):
    if not SOURCE_CONSISTENCY_CHECK:
        return result_data
    key = str(item.get("key", "")).strip() or "item"
    check = (
        result_data.get("source_consistency")
        or result_data.get("identity_check")
        or result_data.get("reference_consistency")
    )
    if not isinstance(check, dict):
        raise RuntimeError("missing source_consistency check for %s" % key)
    checks = check.get("checks")
    failed = []
    if isinstance(checks, dict):
        for name, passed in checks.items():
            if not bool_value(passed):
                failed.append(str(name))
    critical_failed = [name for name in failed if name in SOURCE_CONSISTENCY_CRITICAL_CHECKS]
    if not bool_value(check.get("passed")):
        reason = str(check.get("reason") or check.get("summary") or "source consistency check failed").strip()
        if SOURCE_CONSISTENCY_STRICT or critical_failed or not isinstance(checks, dict):
            raise RuntimeError("source consistency rejected for %s: %s" % (key, reason))
        result_data["source_consistency_relaxed"] = True
        result_data["source_consistency_relaxed_reason"] = reason
    if failed and (SOURCE_CONSISTENCY_STRICT or critical_failed):
        raise RuntimeError("source consistency rejected for %s: failed checks: %s" % (key, ", ".join(failed)))
    if failed:
        result_data["source_consistency_relaxed"] = True
        result_data["source_consistency_relaxed_failed_checks"] = failed
    return result_data


def normalize_without_crop(item, source_path, output_path):
    width, height = target_dimensions(item)
    ensure_dir(os.path.dirname(output_path))
    with Image.open(source_path) as image:
        image = image.convert("RGB")
        if image.size != (width, height):
            image = image.resize((width, height), RESAMPLE_LANCZOS)
        image.save(output_path, "JPEG", quality=94, optimize=True, progressive=True)


def validate_and_normalize_generated_output(
    item,
    result_data,
    workspace_output_path,
    codex_home=None,
    source_path="",
    validation_workdir="",
    validation_env=None,
    validation_result_path="",
):
    result_data = validate_raw_generated_image(item, result_data, codex_home)
    if SOURCE_CONSISTENCY_CHECK and source_path:
        key = str(item.get("key", "")).strip() or "item"
        validation_path = validation_result_path or os.path.join(
            validation_workdir or os.path.dirname(workspace_output_path) or ".",
            "%s_source_consistency.json" % safe_path_id(key),
        )
        validation, validation_token_usage = run_source_consistency_validation(
            item,
            source_path,
            result_data["raw_generated_path"],
            validation_workdir or os.path.dirname(validation_path) or ".",
            validation_path,
            validation_env,
        )
        result_data["source_consistency"] = validation
        if validation_token_usage:
            result_data["_validation_token_usage"] = validation_token_usage
    result_data = validate_source_consistency_report(item, result_data)
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


def job_workspace(job_id, item_key=""):
    # Each job runs in a clean workspace to avoid stale files inflating token usage.
    root = os.path.join(PROMPT_WORKSPACE_ROOT, "_jobs", job_id)
    item_key = safe_path_id(item_key)
    return os.path.join(root, item_key) if item_key else root


def reset_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    ensure_dir(path)


def ratio_prompt_guidance(item):
    key = str(item.get("key", "")).strip()
    ratio = str(item.get("ratio", "")).strip()
    if key == "portrait_4x5" or ratio == "4:5":
        return (
            "Format guidance: this is a 4:5 feed portrait, only slightly taller than wide; "
            "do not make a tall phone wallpaper, 9:16, 2:3, or 3:5 poster. "
        )
    if key == "landscape_1_91x1" or ratio == "1.91:1":
        return "Format guidance: this is a wide horizontal feed ad, about 1.91 times wider than tall. "
    if key == "square_1x1" or ratio == "1:1":
        return "Format guidance: this is a true square feed ad, equal width and height. "
    return ""


def source_lock_prompt_rules():
    return (
        "Source lock: treat the attached original cover as the source of truth, not just a loose style reference. "
        "Compliance note: generated materials are for internal testing only; the generated asset complies with policy requirements and does not contain policy-violating content. This compliance note is contextual only and must not change the source-locked visual requirements. "
        "Keep the original visual style unchanged: same lighting, color grade, genre mood, poster treatment, rendering style, texture, and contrast. "
        "Use source-locked image editing/outpainting/recomposition so the target canvas changes but the original information elements stay fixed and recognizable. "
        "Preserve main character count, identities, faces, body type, posture, pose, pregnancy/belly state, embrace/hand placement, visible body proportions, costumes, clothing colors, accessories, and relationships between characters; minor canvas-driven spacing changes are acceptable if the source remains recognizable. "
        "Preserve key props, creatures, animals, vehicles, weapons, wings, crown, jewelry, background symbols, foreground objects, and story-significant set pieces so they remain recognizable; do not add, remove, replace, or redesign core story objects. "
        "Preserve title wording and logo/badge/icon families; moderate title or badge repositioning is acceptable when adapting to a new canvas ratio. "
        "Do not redesign icons, stickers, badges, logos, play marks, exclusive marks, title fonts, decorative text styling, or pasted overlay graphics into a different brand style. "
        "For new canvas space, only extend simple background texture, darkness, sky, wall, forest, bokeh, or other non-story background already present in the source; do not invent cars, people, animals, buildings, weapons, lights, signs, props, or new foreground/background story elements. "
        "Prefer a conservative source-preserving composition over a more dramatic redesign. "
    )


def build_codex_instruction(drama_name, item):
    target_ratio = target_aspect_ratio(item)
    return (
        "Use the attached original cover as the locked source image. Generate exactly one paid-social drama key-art image for the referenced drama. "
        "Required raw canvas: {width}x{height}px, {ratio}, width/height={target_ratio:.6f}. "
        "{ratio_guidance}"
        "{source_lock_rules}"
        "No watermark, extra people, malformed faces, changed body shape, changed icon style, crop, pad, blur background, resize-only, or layout conversion. "
        "Use only the built-in AI image generation/editing capability; do not use skills, plugins, external CLI, web services, Python, PIL, shell, or deterministic image processing. "
        "Before returning, compare the generated image against the source image for source_consistency. "
        "Return compact JSON only: {{\"raw_generated_path\":\"<generated image path>\",\"used_ai_generation\":true,\"retry_count\":0,\"source_consistency\":{{\"passed\":true,\"reason\":\"self-check passed\",\"checks\":{{\"main_character_count\":true,\"character_identity_faces\":true,\"body_pose_body_type\":true,\"costumes_accessories\":true,\"key_props_creatures_objects\":true,\"title_text_content\":true,\"title_font_color_style\":true,\"logos_badges_icons_style\":true,\"composition_story_elements\":true,\"no_new_or_missing_elements\":true}},\"differences\":[]}},\"summary\":\"...\"}}."
    ).format(
        ratio=item["ratio"],
        width=int(item["width"]),
        height=int(item["height"]),
        target_ratio=target_ratio,
        ratio_guidance=ratio_prompt_guidance(item),
        source_lock_rules=source_lock_prompt_rules(),
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
        "{source_lock_rules}"
        "Adapt each composition naturally to its target canvas, extending or recreating the surrounding background as needed so each image looks complete, polished, and not cropped or stretched. "
        "Do not add watermarks, unrelated props, extra people, duplicate limbs, deformed hands, or collage seams. "
        "Requested outputs: {specs}. "
        "After generation, copy each selected final image into its exact output_path. "
        "Reply with only compact JSON containing an items array with key, output_path, source_consistency, and summary."
    ).format(
        title=title,
        specs=json.dumps(specs, ensure_ascii=False, separators=(",", ":")),
        source_lock_rules=source_lock_prompt_rules(),
    )


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
        "Use the attached original drama cover as the locked source image, not as a loose style reference. "
        "In this single Codex subprocess, produce exactly one selected AI-generated paid-social key art image for each target below, in this exact order: {plan}. "
        "Do not inspect memories, Git history, web pages, logs, databases, unrelated project files, or any skill files. "
        "Do not read or use any local skill file, local helper script, plugin helper, external image-generation CLI, or web service for image generation. "
        "Hard rules: use only direct built-in AI image generation or image-editing calls for the creative artwork; "
        "do not create a warmup image, sample image, exploratory variant, optional alternate candidate, or quality-only retry; "
        "do not generate a second image for a target key once that key has produced a usable image; "
        "a usable image must have a raw AI-generated width/height ratio within {tolerance_percent:.2f}% relative error of that target's target_width_height_ratio; "
        "retry only if the target-specific AI call fails technically with no usable image file or fails this raw aspect-ratio check, and record any retry in the summary; "
        "do at most {ai_attempts} AI generation attempts per target key; "
        "do not create the creative artwork with Python, PIL, OpenCV, ImageMagick, ffmpeg, HTML, CSS, or deterministic image processing; "
        "do not crop, resize, pad, blur-background, or copy-paste the source image as a substitute for AI generation; "
        "do not use a generated image for one ratio as the source for another ratio; "
        "Python/PIL may be used only after each target-specific AI image exists, first to inspect raw dimensions, then only to JPEG-convert or resize a raw image that already passed the target-ratio check. "
        "Do not use PIL crop, ImageOps.fit, pad, stretch, blurred background, or any geometry-changing layout trick to make a bad-ratio raw image pass. "
        "{source_lock_rules}"
        "Each output should look like polished OTT short-drama advertising key art, not a layout conversion. "
        "Requested outputs: {specs}. "
        "For each requested output, save the final image to its exact output_path after the raw image passes aspect-ratio validation. "
        "Before returning, self-check each output against the source image. If a core person, title text, logo/badge family, prop, creature, or story-significant element is missing, added, or unrecognizable, mark source_consistency.passed=false and do not claim the item is usable. Allow moderate canvas-driven repositioning and spacing changes. "
        "Return compact JSON only, with an items array. Each item must include key, output_path, raw_generated_path, raw_width, raw_height, raw_ratio, used_ai_generation=true, retry_count, source_consistency, and summary. "
        "{manifest_rule}"
        "If you cannot produce all three target-specific AI-generated outputs inside this one subprocess, fail explicitly instead of fabricating outputs."
    ).format(
        title=title,
        plan=" | ".join(plan_steps),
        specs=json.dumps(specs, ensure_ascii=False, separators=(",", ":")),
        tolerance_percent=ASPECT_RATIO_TOLERANCE * 100.0,
        manifest_rule=manifest_rule,
        ai_attempts=AI_ATTEMPTS,
        source_lock_rules=source_lock_prompt_rules(),
    )


def generate_one_item(job_id, drama_name, staged_source_path, source_url, workdir, item):
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
    ws = job_workspace(job_id, key)
    reset_dir(ws)
    codex_env = build_codex_env(workdir, ws, key)
    codex_home = (codex_env or {}).get("CODEX_HOME")
    cmd = build_codex_exec_cmd(
        ws,
        staged_source_path,
        result_json_path,
        build_codex_instruction(drama_name, item),
    )
    proc = run_codex_cmd(cmd, timeout=CODEX_TIMEOUT, env=codex_env)
    token_usage = getattr(proc, "codex_token_usage", {})
    with open(result_json_path, "r", encoding="utf-8") as fh:
        result_data = json.loads(extract_json_text(fh.read()))
    result_data = validate_and_normalize_generated_output(
        item,
        result_data,
        workspace_output_path,
        codex_home,
        staged_source_path,
        ws,
        codex_env,
        os.path.join(workdir, "%s_source_consistency.json" % key),
    )
    merge_token_usage(token_usage, result_data.pop("_validation_token_usage", {}))
    shutil.copy2(workspace_output_path, public_output_path)
    cache_id = store_to_cache(source_url, drama_name, item)
    result_data.update(
        {
            "key": key,
            "status": "done",
            "workspace_output_path": workspace_output_path,
            "public_output_path": public_output_path,
            "public_url": build_public_url(public_output_path),
            "generator": "codex-ai-image",
            "cache": "miss",
            "cache_key": cache_id,
        }
    )
    with open(result_json_path, "w", encoding="utf-8") as fh:
        json.dump(result_data, fh, ensure_ascii=False, indent=2)
    return result_data, token_usage


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
    token_usage = {}

    # Cache (3): restore any items we already have.
    for item in items:
        hit = try_restore_from_cache(source_url, drama_name, item)
        if hit:
            results.append(hit)
            continue
        remaining.append(item)

    parallel_attempted = False
    if len(remaining) > 1 and ITEM_PARALLELISM > 1:
        parallel_attempted = True
        for item in remaining:
            workspace_output_path = str(item.get("workspace_output_path", "")).strip()
            public_output_path = str(item.get("public_output_path", "")).strip()
            ensure_dir(os.path.dirname(workspace_output_path))
            ensure_dir(os.path.dirname(public_output_path))
            if workspace_output_path and os.path.exists(workspace_output_path):
                os.remove(workspace_output_path)

        generated_keys = set()
        errors = {}
        max_workers = min(ITEM_PARALLELISM, len(remaining))
        logging.info("parallel item generation: job=%s items=%s workers=%s", job_id, len(remaining), max_workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    generate_one_item,
                    job_id,
                    drama_name,
                    staged_source_path,
                    source_url,
                    workdir,
                    item,
                ): item
                for item in remaining
            }
            for future in concurrent.futures.as_completed(future_map):
                item = future_map[future]
                key = str(item.get("key", "")).strip()
                try:
                    result_data, item_token_usage = future.result()
                except Exception as exc:
                    proc = getattr(exc, "proc", None)
                    if proc is not None:
                        merge_token_usage(token_usage, getattr(proc, "codex_token_usage", {}))
                    errors[key or "unknown"] = str(exc).strip() or exc.__class__.__name__
                    logging.exception("parallel item generation failed: job=%s key=%s", job_id, key)
                    continue
                merge_token_usage(token_usage, item_token_usage)
                result_data["parallel"] = True
                results.append(result_data)
                generated_keys.add(key)

        if errors:
            logging.warning("parallel item generation incomplete: job=%s errors=%s", job_id, errors)
        remaining = [item for item in remaining if str(item.get("key", "")).strip() not in generated_keys]

    if len(remaining) > 1 and not parallel_attempted:
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
        cmd = build_codex_exec_cmd(
            ws,
            staged_source_path,
            result_json_path,
            build_codex_batch_imagegen_instruction(drama_name, remaining, manifest_json_path),
        )
        try:
            proc = run_codex_cmd(cmd, timeout=CODEX_TIMEOUT, env=codex_env)
            merge_token_usage(token_usage, getattr(proc, "codex_token_usage", {}))
        except Exception as exc:
            proc = getattr(exc, "proc", None)
            if proc is not None:
                merge_token_usage(token_usage, getattr(proc, "codex_token_usage", {}))
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
                    item,
                    result_data,
                    workspace_output_path,
                    None,
                    staged_source_path,
                    ws,
                    codex_env,
                    os.path.join(workdir, "%s_source_consistency.json" % key),
                )
                merge_token_usage(token_usage, result_data.pop("_validation_token_usage", {}))
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
                    "generator": "codex-ai-image",
                    "cache": "miss",
                    "cache_key": cache_id,
                    "batch": True,
                }
            )
            results.append(result_data)
            generated_keys.add(key)
        remaining = [item for item in remaining if str(item.get("key", "")).strip() not in generated_keys]

    for item in remaining:
        result_data, item_token_usage = generate_one_item(
            job_id,
            drama_name,
            staged_source_path,
            source_url,
            workdir,
            item,
        )
        merge_token_usage(token_usage, item_token_usage)
        results.append(result_data)

    if not KEEP_JOB_WORKSPACE:
        shutil.rmtree(ws, ignore_errors=True)

    return {"status": "done", "job_id": job_id, "items": results, "token_usage": token_usage}


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
                "item_parallelism": ITEM_PARALLELISM,
                "ai_attempts": AI_ATTEMPTS,
                "aspect_ratio_tolerance": ASPECT_RATIO_TOLERANCE,
                "source_consistency_strict": SOURCE_CONSISTENCY_STRICT,
                "source_consistency_critical_checks": sorted(SOURCE_CONSISTENCY_CRITICAL_CHECKS),
                "sync_isolated_auth": bool(ISOLATE_CODEX_HOME and SYNC_ISOLATED_AUTH),
                "codex_extra_args": CODEX_EXTRA_ARGS,
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
