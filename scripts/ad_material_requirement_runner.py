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


def task_kind(task):
    text = str(task.get("task_type") or "").strip().lower()
    if "参考衍生" in text or "reference" in text:
        return "reference"
    if "素材迭代" in text or "iteration" in text:
        return "iteration"
    if "竞品" in text or "competitor" in text:
        return "competitor"
    return "general"


def pick_provider(task):
    source = str(task.get("competitor_source") or "").strip().lower()
    if task_kind(task) in {"reference", "iteration"} and not source:
        return "local_reference"
    if source in {"\u5e7f\u5927\u5927", "guangdada", "dataidea", "dataidea/guangdada"}:
        return "guangdada"
    if source in {"metapi", "meta api"}:
        return "metapi"
    configured = os.environ.get("AD_MATERIAL_REQUIREMENT_PROVIDER", "").strip().lower()
    if configured:
        return configured
    return "guangdada"


def reference_items(task):
    items = []
    refs = task.get("reference_files") or []
    if not isinstance(refs, list):
        return items
    for index, item in enumerate(refs, 1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or ("reference_%02d" % index)).strip()
        url = str(item.get("url") or item.get("public_url") or "").strip()
        content_type = str(item.get("content_type") or "").strip()
        items.append({
            "index": index,
            "code": "REF_%02d" % index,
            "name": name,
            "url": url,
            "content_type": content_type,
        })
    return items


def output_slots(size_plan):
    slots = []
    cursor = 1
    for item in size_plan:
        size = str(item.get("size") or "1:1").strip()
        count = max(0, int(item.get("count") or 0))
        for _ in range(count):
            slots.append({"index": cursor, "size": size, "dimensions": normalize_size(size)})
            cursor += 1
    return slots


def local_reference_artifacts(task_id):
    work_root = Path(os.environ.get("AD_MATERIAL_WORK_ROOT", "/root/ad_material_tasks"))
    public_root = Path(os.environ.get("AD_MATERIAL_PUBLIC_ROOT", "/usr/share/nginx/html/ad-materials"))
    public_base = os.environ.get("AD_MATERIAL_PUBLIC_BASE_URL", "https://ai.yingliangads.com/ad-materials").rstrip("/")
    export_dir = public_root / task_id / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = export_dir / ("%s_reference_requirement.md" % task_id[:8])
    markdown_url = "%s/%s/exports/%s" % (public_base, task_id, markdown_path.name)
    evidence_path = work_root / task_id / "reference_requirement_evidence.json"
    return markdown_path, markdown_url, evidence_path


def build_local_reference_demand(task, size_plan, revision_instruction=""):
    product = str(task.get("product_name") or task.get("app_id") or "未命名产品").strip()
    task_id = str(task.get("task_id") or "").strip()
    country = str(task.get("country") or "").strip()
    language = str(task.get("language") or "").strip()
    description = str(task.get("description") or "").strip()
    kind = task_kind(task)
    refs = reference_items(task)
    slots = output_slots(size_plan)
    quantity = len(slots) or max(1, int(task.get("quantity") or 1))
    size_summary = ", ".join("%s x %s" % (item.get("size"), item.get("count")) for item in size_plan) or str(task.get("size") or "")
    title = str(task.get("title") or "").strip()
    body = str(task.get("body") or "").strip()
    tag_name = str(task.get("tag_name") or "").strip()
    category = str(task.get("category") or "").strip()
    store_url = str(task.get("store_url") or "").strip()
    product_icon_url = str(task.get("product_icon_url") or "").strip()

    mode_text = "参考衍生" if kind == "reference" else "素材迭代"
    lines = [
        "# %s 投放素材需求" % product,
        "",
        "## 任务参数",
        "",
        "- 任务类型：%s" % (task.get("task_type") or mode_text),
        "- 产品：%s" % product,
        "- 国家/语言：%s / %s" % (country or "未填写", language or "未填写"),
        "- 尺寸与数量：%s" % (size_summary or "未填写"),
        "- 输出数量：%s 张静态图片" % quantity,
        "- 竞品接口：不使用。该任务为%s任务，需求文档只允许使用用户上传参考素材、用户描述和产品基础信息。" % mode_text,
        "- 用户需求描述：%s" % (description or "未填写；默认按上传参考素材做风格迁移与同类新图。"),
    ]
    if title or body or category or tag_name:
        lines.extend([
            "- 上报标签 tag_name：%s" % tag_name,
            "- category：%s" % category,
            "- title：%s" % title,
            "- body：%s" % body,
        ])
    if store_url or product_icon_url:
        lines.extend(["", "## 产品基础信息", ""])
        if store_url:
            lines.append("- 商店链接：%s" % store_url)
        if product_icon_url:
            lines.append("- 产品图标：%s" % product_icon_url)
            lines.append("")
            lines.append('<img src="%s" width="96">' % product_icon_url)

    lines.extend([
        "",
        "## 参考素材",
        "",
    ])
    if refs:
        lines.extend([
            "以下参考素材是本需求的核心输入。生成需求和后续生图必须先解析这些图的版式、色彩、主体、金额/利益点层级、CTA 位置和免责声明位置，再迁移到当前产品与目标语言；不得拉取或混入任何竞品接口素材。",
            "",
            "| 编号 | 上传文件 | 预览 | 需要继承 | 必须规避 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for ref in refs:
            preview = '<img src="%s" width="180">' % ref["url"] if ref["url"] else ""
            inherit = "构图比例、主标题层级、金额/利益点大字、卡片式信息区、CTA 按钮位置、蓝白金融视觉节奏。"
            avoid = "不得照搬原图品牌、金额、币种、承诺、Logo、合规标识或原图中的不可验证还款数据。"
            lines.append("| %s | %s | %s | %s | %s |" % (ref["code"], ref["name"], preview, inherit, avoid))
    else:
        lines.extend([
            "- 未检测到上传参考素材。该任务不能自动改走竞品接口；请补充参考素材或在任务描述中明确画面结构。",
        ])

    lines.extend([
        "",
        "## 参考素材解析要求",
        "",
        "- 先看上传参考图，再写每张图的制作要求；不得用固定竞品模板替代参考图。",
        "- 视觉上优先保留参考图的高转化结构：大标题、大金额/核心利益点、信息卡片、强 CTA、底部免责声明。",
        "- 内容上必须替换为当前产品、当前国家和当前语言；原参考图里的产品名、币种、金额、审批速度、还款数据不能原样继承。",
        "- 若用户描述与参考图冲突，以用户描述优先；若用户描述较短，则以参考图的可迁移版式和信息层级补足。",
        "- Logo 必须使用当前产品 Logo 或预留后置叠加位置；不得让模型重新绘制相似 Logo。",
        "- 文案必须可编辑、清晰、不乱码；关键文字建议由后置图层叠加。",
        "",
        "## 逐张素材需求",
        "",
    ])

    ref_codes = ", ".join(ref["code"] for ref in refs) if refs else "上传参考素材"
    for slot in slots or [{"index": i, "size": str(task.get("size") or "1:1"), "dimensions": normalize_size(task.get("size"))} for i in range(1, quantity + 1)]:
        index = int(slot["index"])
        size = slot["size"]
        dimensions = slot["dimensions"]
        if index % 2 == 1:
            angle = "以大标题和核心金额/利益点为第一视觉，右侧或中部放金融视觉元素，底部放信息卡与 CTA。"
            focus = "强冲击首屏：让用户先看到需求/利益点，再看到可行动按钮。"
        else:
            angle = "以 App 计算器/额度卡片结构为主，中央用白色圆角卡片承载金额、周期、说明和 CTA。"
            focus = "清晰解释型：让用户理解产品能力、申请入口和注意事项。"
        lines.extend([
            "### 素材 %02d" % index,
            "",
            "- 输出规格：%s（%s）" % (size, dimensions),
            "- 主参考素材：%s" % ref_codes,
            "- 需求目标：%s" % (description or "参考上传素材做同风格迭代，生成当前产品可用的新静态图。"),
            "- 构图方向：%s" % angle,
            "- 表达重点：%s" % focus,
            "- 画面要求：蓝白为主、信息层级清楚、金额/利益点字号最大、CTA 高对比、底部保留免责声明安全区；允许保留参考图的卡片/滑杆/图标节奏，但必须更换为当前产品内容。",
            "- 文案要求：使用 %s 语言；不得直接复制参考图的西语文案、MXN 币种、审批分钟数、具体还款金额或月供。" % (language or "目标市场"),
            "- 生成提示：先复刻参考图的版式骨架和视觉节奏，再替换品牌、语言、产品事实和图标；图片中不出现竞品名、竞品 Logo 或竞品 UI。",
            "- 验收标准：尺寸正确；能看出来自上传参考素材的风格衍生；没有竞品素材、没有外部接口素材、没有乱码文字、没有未验证金融承诺。",
            "",
        ])

    lines.extend([
        "## 禁止项",
        "",
        "- 禁止调用广大大、MetApi、有米云等竞品接口补充素材。",
        "- 禁止出现参考图原品牌、竞品 Logo、二维码、商店按钮、真实证件、银行卡、OTP、联系人或催收压力表达。",
        "- 禁止承诺秒批、必过、无审核、立即到账、固定月供、固定总还款额，除非用户或产品资料明确提供。",
        "- 禁止把参考图简单换色、换字后直接交付；必须做当前产品的新构图或新组合。",
    ])
    if revision_instruction:
        lines.extend(["", "## 本次重新生成说明", "", revision_instruction])
    return "\n".join(lines).rstrip() + "\n"


def build_and_write_local_reference_output(task, payload, size_plan, output_path):
    revision_instruction = str((payload.get("extra") or {}).get("reason") or task.get("review_reason") or "").strip()
    demand_text = build_local_reference_demand(task, size_plan, revision_instruction)
    task_id = str(task.get("task_id") or os.environ.get("AD_MATERIAL_TASK_ID") or "ad_material").strip()
    markdown_path, markdown_url, evidence_path = local_reference_artifacts(task_id)
    markdown_path.write_text(demand_text, encoding="utf-8")
    evidence = {
        "provider": "local_reference",
        "task_kind": task_kind(task),
        "competitor_source": task.get("competitor_source") or "",
        "reference_files": reference_items(task),
        "size_plan": size_plan,
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    write_output(output_path, {
        "demand_text": demand_text,
        "markdown": demand_text,
        "provider": "local_reference",
        "artifacts": {
            "provider": "local_reference",
            "markdown_path": str(markdown_path),
            "markdown_url": markdown_url,
            "evidence_path": str(evidence_path),
            "internal_refs": str(len(evidence["reference_files"])),
            "competitor_refs": "0",
            "selected_competitors": "",
        },
    })

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
    size_plan = parse_size_plan(task)
    if provider == "local_reference":
        build_and_write_local_reference_output(task, payload, size_plan, output_path)
        return

    skill_root = Path(os.environ.get("CODEX_SKILLS_ROOT", "/root/.codex/skills"))
    script = skill_root / ("image-material-requirements-%s" % provider) / "scripts" / "generate_image_material_brief.py"
    if not script.exists():
        raise SystemExit("Requirement skill script not found: %s" % script)
    script_text = script.read_text(encoding="utf-8", errors="ignore")

    today = date.today()
    default_start = today - timedelta(days=33)
    default_end = today - timedelta(days=3)
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
    result["provider"] = provider
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
