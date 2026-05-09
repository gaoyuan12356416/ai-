#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_screenshot_service import (
    ASPECT_RATIO_TOLERANCE,
    build_codex_batch_imagegen_instruction,
    extract_json_text,
)


SPECS = [
    {
        "key": "square_1x1",
        "label": "1:1",
        "ratio": "1:1",
        "width": 1200,
        "height": 1200,
        "filename": "square_1x1.jpg",
    },
    {
        "key": "landscape_1_91x1",
        "label": "1.91:1",
        "ratio": "1.91:1",
        "width": 1200,
        "height": 628,
        "filename": "landscape_1_91x1.jpg",
    },
    {
        "key": "portrait_4x5",
        "label": "4:5",
        "ratio": "4:5",
        "width": 1200,
        "height": 1500,
        "filename": "portrait_4x5.jpg",
    },
]


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def copy_source(source_path, job_root):
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    source_dir = job_root / "source"
    ensure_dir(source_dir)
    suffix = source.suffix or ".jpg"
    staged = source_dir / ("cover_source" + suffix)
    shutil.copy2(source, staged)
    return staged


def build_items(job_root):
    generated_dir = job_root / "generated"
    ensure_dir(generated_dir)
    items = []
    for spec in SPECS:
        item = dict(spec)
        item["workspace_output_path"] = str((generated_dir / spec["filename"]).resolve())
        items.append(item)
    return items


def toml_string(value):
    return json.dumps(str(value))


def prepare_isolated_codex_home(job_root):
    source_home = Path(
        os.environ.get("CODEX_SCREENSHOT_SOURCE_CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    codex_home = job_root / "codex_home"
    if codex_home.exists():
        shutil.rmtree(str(codex_home))
    ensure_dir(codex_home)
    for dirname in ("generated_images", "sessions", "log", "tmp", "shell_snapshots"):
        ensure_dir(codex_home / dirname)
    # Codex auto-installs bundled system skills into CODEX_HOME/skills when it
    # can create that directory. For this test, the server-side subprocess must
    # not expose the imagegen skill at all, so keep the path as a plain file.
    (codex_home / "skills").write_text("system skills disabled for screenshot batch test\n", encoding="utf-8")
    for filename in (
        "auth.json",
        "installation_id",
        "version.json",
        "models_cache.json",
        ".personality_migration",
    ):
        source = source_home / filename
        if source.is_file():
            shutil.copy2(str(source), str(codex_home / filename))
    model = os.environ.get("CODEX_SCREENSHOT_TEST_MODEL", "gpt-5.5")
    reasoning = os.environ.get("CODEX_SCREENSHOT_TEST_REASONING", "medium")
    config = "\n".join(
        [
            "model = %s" % toml_string(model),
            "model_reasoning_effort = %s" % toml_string(reasoning),
            'approval_policy = "never"',
            "",
            "[projects.%s]" % toml_string(str(job_root)),
            'trust_level = "trusted"',
            "",
            "[notice]",
            "hide_full_access_warning = true",
            "fast_default_opt_out = true",
            "",
        ]
    )
    (codex_home / "config.toml").write_text(config, encoding="utf-8")
    return codex_home


def run_codex(args, job_root, source_path, items, prompt):
    result_path = job_root / "result.json"
    events_path = job_root / "codex_events.jsonl"
    stderr_path = job_root / "codex_stderr.log"
    codex_home = prepare_isolated_codex_home(job_root)
    cmd = [
        args.codex_bin,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(job_root),
        "-i",
        str(source_path),
        "-o",
        str(result_path),
        prompt,
    ]
    started = time.time()
    with events_path.open("w", encoding="utf-8") as stdout_fh, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_fh:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        proc = subprocess.run(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_fh,
            stderr=stderr_fh,
            universal_newlines=True,
            timeout=args.timeout,
        )
    duration = time.time() - started
    if proc.returncode != 0:
        raise RuntimeError(
            "codex exec failed with code %s; see %s" % (proc.returncode, stderr_path)
        )
    return result_path, events_path, stderr_path, duration, codex_home


def parse_result(result_path, manifest_path):
    raw = ""
    if result_path.is_file():
        raw = result_path.read_text(encoding="utf-8", errors="replace")
    if not raw.strip() and manifest_path.is_file():
        raw = manifest_path.read_text(encoding="utf-8", errors="replace")
    if not raw.strip():
        raise RuntimeError("Codex did not write result JSON")
    return json.loads(extract_json_text(raw))


def count_image_generation_events(events_path):
    count = 0
    call_ids = set()
    if not events_path.is_file():
        return count, []
    with events_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "image_generation" not in line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            payload = event.get("payload") or event
            event_type = payload.get("type") or event.get("type")
            if event_type == "image_generation_end":
                count += 1
                call_id = payload.get("call_id")
                if call_id:
                    call_ids.add(call_id)
    return count, sorted(call_ids)


def parse_codex_thread_id(events_path):
    if not events_path.is_file():
        return ""
    with events_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("type") == "thread.started":
                return str(event.get("thread_id") or "")
    return ""


def assert_no_forbidden_context(events_path):
    if not events_path.is_file():
        return
    forbidden = [
        "/.codex/skills/",
        "\\.codex\\skills\\",
        "/skills/.system/",
        "\\skills\\.system\\",
        "SKILL.md",
        "MEMORY.md",
    ]
    with events_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_number, line in enumerate(fh, 1):
            for marker in forbidden:
                if marker in line:
                    raise RuntimeError(
                        "forbidden context access found in Codex event log at line %s: %s"
                        % (line_number, marker)
                    )


def find_codex_session_path(codex_home, thread_id):
    if not thread_id:
        return None
    sessions_dir = Path(codex_home) / "sessions"
    if not sessions_dir.is_dir():
        return None
    matches = list(sessions_dir.rglob("*%s*.jsonl" % thread_id))
    if matches:
        return sorted(matches, key=lambda path: str(path))[-1]
    return None


def parse_session_evidence(session_path):
    evidence = {
        "session_path": str(session_path) if session_path else "",
        "image_generation_event_count": 0,
        "image_generation_call_ids": [],
        "forbidden_imagegen_context_hits": [],
        "forbidden_tool_access_hits": [],
    }
    if not session_path or not Path(session_path).is_file():
        return evidence
    call_ids = set()
    forbidden_imagegen_context = [
        "image_gen.py",
        "- imagegen:",
        "imagegen/SKILL.md",
        "/skills/.system/imagegen",
        "\\skills\\.system\\imagegen",
    ]
    forbidden_tool_access = [
        "image_gen.py",
        "imagegen/SKILL.md",
        "/imagegen/",
        "/skills/.system/imagegen",
        "\\skills\\.system\\imagegen",
    ]
    with Path(session_path).open("r", encoding="utf-8", errors="replace") as fh:
        for line_number, line in enumerate(fh, 1):
            lower_line = line.lower()
            for marker in forbidden_imagegen_context:
                if marker.lower() in lower_line:
                    evidence["forbidden_imagegen_context_hits"].append(
                        {"line": line_number, "marker": marker}
                    )
                    break
            try:
                event = json.loads(line)
            except Exception:
                continue
            payload = event.get("payload") or {}
            if event.get("type") == "event_msg" and payload.get("type") == "image_generation_end":
                evidence["image_generation_event_count"] += 1
                call_id = payload.get("call_id")
                if call_id:
                    call_ids.add(str(call_id))
            if event.get("type") != "response_item":
                continue
            item = payload.get("item") or payload
            if item.get("type") != "function_call":
                continue
            needle = "%s %s" % (item.get("name") or "", item.get("arguments") or "")
            lower_needle = needle.lower()
            for marker in forbidden_tool_access:
                if marker.lower() in lower_needle:
                    evidence["forbidden_tool_access_hits"].append(
                        {"line": line_number, "marker": marker}
                    )
                    break
    evidence["image_generation_call_ids"] = sorted(call_ids)
    return evidence


def validate_outputs(items, result_data):
    by_key = {
        str(item.get("key")): item
        for item in (result_data.get("items") or [])
        if isinstance(item, dict)
    }
    validations = []
    raw_paths = []
    for item in items:
        path = Path(item["workspace_output_path"])
        if not path.is_file():
            raise RuntimeError("missing output: %s" % path)
        with Image.open(path) as image:
            size = image.size
        expected = (int(item["width"]), int(item["height"]))
        if size != expected:
            raise RuntimeError("bad size for %s: %s expected %s" % (item["key"], size, expected))
        result_item = by_key.get(item["key"], {})
        raw_generated_path = str(result_item.get("raw_generated_path") or "").strip()
        if not raw_generated_path:
            raise RuntimeError("missing raw_generated_path for %s" % item["key"])
        if not Path(raw_generated_path).is_file():
            raise RuntimeError("raw generated file does not exist for %s: %s" % (item["key"], raw_generated_path))
        with Image.open(raw_generated_path) as raw_image:
            raw_width, raw_height = raw_image.size
        target_ratio = int(item["width"]) / float(int(item["height"]))
        raw_ratio = raw_width / float(raw_height)
        raw_ratio_error = abs(raw_ratio - target_ratio) / target_ratio
        if raw_ratio_error > ASPECT_RATIO_TOLERANCE:
            raise RuntimeError(
                "bad raw ratio for %s: %sx%s ratio %.6f expected %.6f tolerance %.2f%%"
                % (
                    item["key"],
                    raw_width,
                    raw_height,
                    raw_ratio,
                    target_ratio,
                    ASPECT_RATIO_TOLERANCE * 100.0,
                )
            )
        raw_paths.append(raw_generated_path)
        validations.append(
            {
                "key": item["key"],
                "path": str(path),
                "size": "%sx%s" % size,
                "raw_generated_path": raw_generated_path,
                "raw_size": "%sx%s" % (raw_width, raw_height),
                "raw_ratio": round(raw_ratio, 10),
                "target_ratio": round(target_ratio, 10),
                "raw_ratio_error": round(raw_ratio_error, 10),
                "used_ai_generation": result_item.get("used_ai_generation"),
                "retry_count": result_item.get("retry_count"),
            }
        )
    if len(set(raw_paths)) != len(raw_paths):
        raise RuntimeError("raw_generated_path values are not unique")
    return validations


def count_thread_generated_files(raw_paths, thread_id):
    if not raw_paths:
        return 0
    first_parent = Path(raw_paths[0]).parent
    if thread_id and first_parent.name != thread_id:
        return 0
    try:
        return len([path for path in first_parent.iterdir() if path.is_file()])
    except Exception:
        return 0


def make_contact_sheet(items, output_path):
    thumbs = []
    for item in items:
        with Image.open(item["workspace_output_path"]).convert("RGB") as image:
            thumb = image.copy()
        thumb.thumbnail((360, 360))
        canvas = Image.new("RGB", (390, 430), "white")
        canvas.paste(thumb, ((390 - thumb.width) // 2, 20))
        draw = ImageDraw.Draw(canvas)
        draw.text((12, 392), "%s %sx%s" % (item["label"], item["width"], item["height"]), fill=(0, 0, 0))
        thumbs.append(canvas)
    sheet = Image.new("RGB", (390 * len(thumbs), 430), "white")
    for index, thumb in enumerate(thumbs):
        sheet.paste(thumb, (390 * index, 0))
    sheet.save(output_path, quality=92)


def main():
    parser = argparse.ArgumentParser(
        description="Run an isolated one-Codex-subprocess test that generates three screenshot sizes."
    )
    parser.add_argument("--source", required=True, help="Source cover image path.")
    parser.add_argument("--drama-name", default="the provided drama")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--work-root", default=str(ROOT / ".test_screenshot_batch"))
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_SCREENSHOT_CODEX_BIN", "codex"))
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    job_id = args.job_id.strip() or ("test_%d" % int(time.time()))
    job_root = Path(args.work_root).expanduser().resolve() / job_id
    if job_root.exists():
        shutil.rmtree(job_root)
    ensure_dir(job_root)

    staged_source = copy_source(args.source, job_root)
    items = build_items(job_root)
    manifest_path = job_root / "manifest.json"
    prompt = build_codex_batch_imagegen_instruction(args.drama_name, items, str(manifest_path))
    prompt_path = job_root / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    if args.dry_run:
        print(json.dumps({"job_root": str(job_root), "prompt_path": str(prompt_path)}, indent=2))
        return

    result_path, events_path, stderr_path, duration, codex_home = run_codex(
        args, job_root, staged_source, items, prompt
    )
    result_data = parse_result(result_path, manifest_path)
    assert_no_forbidden_context(events_path)
    validations = validate_outputs(items, result_data)
    image_event_count, image_call_ids = count_image_generation_events(events_path)
    thread_id = parse_codex_thread_id(events_path)
    session_path = find_codex_session_path(codex_home, thread_id)
    session_evidence = parse_session_evidence(session_path)
    if session_evidence["forbidden_imagegen_context_hits"]:
        raise RuntimeError(
            "forbidden imagegen context found in isolated Codex session: %s"
            % session_evidence["forbidden_imagegen_context_hits"][:5]
        )
    if session_evidence["forbidden_tool_access_hits"]:
        raise RuntimeError(
            "forbidden skill or memory access found in tool calls: %s"
            % session_evidence["forbidden_tool_access_hits"][:5]
        )
    if session_evidence["image_generation_event_count"] < len(items):
        raise RuntimeError(
            "expected at least %s built-in AI image-generation events, found %s"
            % (len(items), session_evidence["image_generation_event_count"])
        )
    selected_raw_paths = [item["raw_generated_path"] for item in validations]
    thread_raw_count = count_thread_generated_files(selected_raw_paths, thread_id)
    contact_sheet = job_root / "contact_sheet.jpg"
    make_contact_sheet(items, contact_sheet)

    summary = {
        "status": "done",
        "job_root": str(job_root),
        "duration_seconds": round(duration, 1),
        "result_path": str(result_path),
        "manifest_path": str(manifest_path),
        "events_path": str(events_path),
        "stderr_path": str(stderr_path),
        "isolated_codex_home": str(codex_home),
        "contact_sheet": str(contact_sheet),
        "codex_thread_id": thread_id,
        "codex_session_evidence": session_evidence,
        "codex_json_image_generation_event_count": image_event_count,
        "codex_json_image_generation_call_ids": image_call_ids,
        "selected_raw_generated_count": len(set(selected_raw_paths)),
        "thread_raw_generated_file_count": thread_raw_count,
        "verification_note": (
            "Codex JSON stdout may not expose built-in image-generation events; "
            "the test validates selected raw generated files from the manifest instead."
        ),
        "outputs": validations,
    }
    (job_root / "test_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
