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


def parse_size_plan(task):
    raw_plan = task.get("size_plan")
    if isinstance(raw_plan, list):
        result = []
        for item in raw_plan:
            if not isinstance(item, dict):
                continue
            size = str(item.get("size") or "").strip()
            count = int(item.get("count") or 0)
            if size and count > 0:
                result.append({"size": size, "count": count})
        if result:
            return result
    text = str(task.get("size") or "").strip()
    quantity = max(1, min(20, int(task.get("quantity") or 1)))
    if not text:
        return [{"size": "1:1", "count": quantity}]
    plan = []
    for part in re.split(r"[,，;；\n]+", text):
        part = part.strip()
        if not part:
            continue
        size_match = re.search(r"(1\.91:1|1:1|4:5|9:16|16:9|\d{3,5}x\d{3,5})", part, flags=re.I)
        if not size_match:
            continue
        count_match = re.search(r"(?:x|×|\*)\s*(\d+)|(\d+)\s*(?:张|条|个)", part, flags=re.I)
        count = int(next((group for group in (count_match.groups() if count_match else []) if group), "1"))
        if count > 0:
            plan.append({"size": size_match.group(1), "count": count})
    return plan or [{"size": "1:1", "count": quantity}]


def append_size_plan(demand_text, size_plan):
    if not size_plan:
        return demand_text
    lines = ["", "## 尺寸与数量计划", ""]
    cursor = 1
    for item in size_plan:
        size = str(item.get("size") or "").strip()
        count = int(item.get("count") or 0)
        for _ in range(max(0, count)):
            lines.append("- 素材 %02d：%s" % (cursor, size))
            cursor += 1
    if cursor == 1:
        return demand_text
    return demand_text.rstrip() + "\n" + "\n".join(lines) + "\n"


def pick_provider(task):
    source = str(task.get("competitor_source") or "").strip().lower()
    if source in {"\u5e7f\u5927\u5927", "guangdada", "dataidea", "dataidea/guangdada"}:
        return "guangdada"
    if source in {"metapi", "meta api"}:
        return "metapi"
    configured = os.environ.get("AD_MATERIAL_REQUIREMENT_PROVIDER", "").strip().lower()
    if configured:
        return configured
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
            "pdf_path",
            "pdf_url",
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
    size_plan = parse_size_plan(task)
    plan_quantity = sum(max(0, int(item.get("count") or 0)) for item in size_plan)
    quantity = max(1, min(20, plan_quantity or int(task.get("quantity") or 1)))
    primary_size = size_plan[0]["size"] if size_plan else task.get("size")
    revision_instruction = str((payload.get("extra") or {}).get("reason") or task.get("review_reason") or "").strip()
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
        normalize_size(primary_size),
        "--output-dir",
        str(out_dir),
    ]
    if "--size-plan-json" in script_text:
        cmd.extend(["--size-plan-json", json.dumps(size_plan, ensure_ascii=False)])
    if revision_instruction and "--revision-instruction" in script_text:
        cmd.extend(["--revision-instruction", revision_instruction])
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
    if revision_instruction:
        env["AD_MATERIAL_REVISION_INSTRUCTION"] = revision_instruction
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

    demand_text = append_size_plan(Path(markdown_path).read_text(encoding="utf-8"), size_plan)
    write_output(output_path, {
        "demand_text": demand_text,
        "markdown": demand_text,
        "provider": provider,
        "artifacts": result,
    })


if __name__ == "__main__":
    main()
