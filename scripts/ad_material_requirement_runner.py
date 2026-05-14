#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path


def normalize_size(value):
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return "1080x1080"
    ratio_map = {
        "1:1": "1080x1080",
        "4:5": "1200x1500",
        "9:16": "1080x1920",
        "16:9": "1920x1080",
        "1.91:1": "1200x628",
    }
    if text in ratio_map:
        return ratio_map[text]
    if re.match(r"^\d{3,5}x\d{3,5}$", text):
        return text
    return "1080x1080"


def pick_provider(task):
    configured = os.environ.get("AD_MATERIAL_REQUIREMENT_PROVIDER", "").strip().lower()
    if configured:
        return configured
    source = str(task.get("competitor_source") or "").strip().lower()
    if source in {"\u5e7f\u5927\u5927", "guangdada", "dataidea", "dataidea/guangdada"}:
        return "guangdada"
    if source in {"metapi", "meta api"}:
        return "metapi"
    return "guangdada"


def parse_command_output(stdout):
    result = {}
    for raw in stdout.splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in {
            "json_path",
            "json_url",
            "markdown_path",
            "markdown_url",
            "evidence_path",
            "evidence_url",
            "internal_refs",
            "competitor_refs",
            "selected_competitors",
        }:
            result[key] = value
    return result


def write_output(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    payload_path = os.environ.get("AD_MATERIAL_TASK_PAYLOAD")
    output_path = os.environ.get("AD_MATERIAL_TASK_OUTPUT")
    if not payload_path or not output_path:
        raise SystemExit("AD_MATERIAL_TASK_PAYLOAD and AD_MATERIAL_TASK_OUTPUT are required")

    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    task = payload.get("task") or {}
    provider = pick_provider(task)
    skill_root = Path(os.environ.get("CODEX_SKILLS_ROOT", "/root/.codex/skills"))
    script = skill_root / ("image-material-requirements-%s" % provider) / "scripts" / "generate_image_material_brief.py"
    if not script.exists():
        raise SystemExit("Requirement skill script not found: %s" % script)
    script_text = script.read_text(encoding="utf-8", errors="ignore")

    today = date.today()
    default_start = today - timedelta(days=33)
    default_end = today - timedelta(days=3)
    quantity = max(1, min(20, int(task.get("quantity") or 1)))
    out_dir = Path(os.environ.get("AD_MATERIAL_REQUIREMENT_OUTPUT_DIR", "/root/codex_test/output"))
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(script),
        "--product",
        str(task.get("product_name") or task.get("app_id") or "MexiCash"),
        "--country",
        str(task.get("country") or "MX"),
        "--language",
        str(task.get("language") or "es"),
        "--dt-start",
        os.environ.get("AD_MATERIAL_REQUIREMENT_DT_START", default_start.isoformat()),
        "--dt-end",
        os.environ.get("AD_MATERIAL_REQUIREMENT_DT_END", default_end.isoformat()),
        "--count",
        str(quantity),
        "--size",
        normalize_size(task.get("size")),
        "--output-dir",
        str(out_dir),
    ]
    vision_json = os.environ.get("AD_MATERIAL_VISION_ANALYSIS_JSON", "").strip()
    if vision_json:
        cmd.extend(["--vision-analysis-json", vision_json])
    if "--store-url" in script_text and task.get("store_url"):
        cmd.extend(["--store-url", str(task.get("store_url") or "")])
    if "--package-name" in script_text and task.get("package_name"):
        cmd.extend(["--package-name", str(task.get("package_name") or "")])
    if "--product-icon-url" in script_text and task.get("product_icon_url"):
        cmd.extend(["--product-icon-url", str(task.get("product_icon_url") or "")])

    env = os.environ.copy()
    if task.get("store_url"):
        env["AD_MATERIAL_STORE_URL"] = str(task.get("store_url") or "")
    if task.get("package_name"):
        env["AD_MATERIAL_PACKAGE_NAME"] = str(task.get("package_name") or "")
    if task.get("product_icon_url"):
        env["AD_MATERIAL_PRODUCT_ICON_URL"] = str(task.get("product_icon_url") or "")
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=int(os.environ.get("AD_MATERIAL_REQUIREMENT_RUNNER_TIMEOUT", "1800")),
        env=env,
    )
    if proc.returncode != 0:
        raise SystemExit((proc.stderr or proc.stdout or "requirement generator failed").strip())

    result = parse_command_output(proc.stdout)
    markdown_path = result.get("markdown_path")
    if not markdown_path or not Path(markdown_path).exists():
        raise SystemExit("Requirement generator did not return a readable markdown_path")

    demand_text = Path(markdown_path).read_text(encoding="utf-8")
    write_output(output_path, {
        "demand_text": demand_text,
        "markdown": demand_text,
        "provider": provider,
        "artifacts": result,
    })


if __name__ == "__main__":
    main()
