#!/usr/bin/env python3
import json
import importlib.util
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
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
    kind = task_kind(task)
    if kind == "iteration" and not source:
        return "local_iteration"
    if kind == "reference" and not source:
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


def local_task_artifacts(task_id, suffix):
    work_root = Path(os.environ.get("AD_MATERIAL_WORK_ROOT", "/root/ad_material_tasks"))
    public_root = Path(os.environ.get("AD_MATERIAL_PUBLIC_ROOT", "/usr/share/nginx/html/ad-materials"))
    public_base = os.environ.get("AD_MATERIAL_PUBLIC_BASE_URL", "https://ai.yingliangads.com/ad-materials").rstrip("/")
    export_dir = public_root / task_id / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = export_dir / ("%s_%s_requirement.md" % (task_id[:8], suffix))
    markdown_url = "%s/%s/exports/%s" % (public_base, task_id, markdown_path.name)
    evidence_path = work_root / task_id / ("%s_requirement_evidence.json" % suffix)
    return markdown_path, markdown_url, evidence_path


def load_skill_module(provider):
    skill_root = Path(os.environ.get("CODEX_SKILLS_ROOT", "/root/.codex/skills"))
    script = skill_root / ("image-material-requirements-%s" % provider) / "scripts" / "generate_image_material_brief.py"
    if not script.exists():
        raise RuntimeError("Requirement skill script not found: %s" % script)
    spec = importlib.util.spec_from_file_location("image_material_%s" % provider, str(script))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_slug(value):
    text = re.sub(r"[^0-9A-Za-z_-]+", "_", str(value or "").strip()).strip("_").lower()
    return text or "product"


def collect_internal_winners(task, limit=6):
    product = str(task.get("product_name") or task.get("app_id") or "").strip()
    if not product:
        return [], {"error": "missing product"}
    today = date.today()
    default_start = today - timedelta(days=33)
    default_end = today - timedelta(days=3)
    dt_start = os.environ.get("AD_MATERIAL_REQUIREMENT_DT_START", default_start.isoformat())
    dt_end = os.environ.get("AD_MATERIAL_REQUIREMENT_DT_END", default_end.isoformat())
    out_dir = Path(os.environ.get("AD_MATERIAL_REQUIREMENT_OUTPUT_DIR", "/root/codex_test/output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        module = load_skill_module("guangdada")
        base_mod = module.ensure_base_loaded()
        base_mod.SINCE = dt_start
        base_mod.UNTIL = dt_end
        base_mod.PRODUCT = product
        db = base_mod.db_data()
        stamp = module.now_stamp() if hasattr(module, "now_stamp") else date.today().strftime("%Y%m%d")
        work_dir = out_dir / ("%s_internal_iteration_refs_%s" % (make_slug(product), stamp))
        work_dir.mkdir(parents=True, exist_ok=True)
        refs = module.archive_internal_refs(db, stamp, work_dir, limit=limit)
        summary = module.summarize_internal(db) if hasattr(module, "summarize_internal") else {}
        return refs, {
            "product": product,
            "dt_start": dt_start,
            "dt_end": dt_end,
            "summary": summary,
            "work_dir": str(work_dir),
        }
    except Exception as exc:
        return [], {"product": product, "dt_start": dt_start, "dt_end": dt_end, "error": str(exc)[:500]}


def internal_ref_to_visual_ref(ref):
    asset_id = str(ref.get("asset_id") or "").strip() or "INT_REF"
    return {
        "index": asset_id,
        "code": asset_id,
        "name": str(ref.get("label") or ref.get("created_data_id") or asset_id).strip(),
        "url": str(ref.get("archive_url") or "").strip(),
        "content_type": "image/jpeg",
    }


def compact_text(value, limit=180):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


NOISE_SECTION_TITLES = (
    "data basis",
    "competitor ranking",
    "数据依据",
    "竞品排名",
    "上报字段",
    "审核关注点",
    "本次重新生成说明",
    "本次重生成原因",
    "本轮重做重点",
)

NOISE_LINE_TERMS = (
    "竞品接口",
    "竞品数据源",
    "竞品查询源",
    "竞品来源：",
    "禁止调用",
    "外部接口素材",
    "接口补充素材",
    "metapi",
    "guangdada",
    "广大大",
    "有米云",
    "provider",
    "competitor_refs",
    "selected_competitors",
    "internal_refs",
    "需求通过",
    "需求待审核",
    "审核要求",
    "驳回",
    "后台",
    "触发任务",
    "任务ID",
    "remark：",
)


def is_noise_line(line):
    normalized = str(line or "").strip().lower()
    if not normalized:
        return False
    return any(term.lower() in normalized for term in NOISE_LINE_TERMS)


def clean_material_demand_text(text):
    lines = str(text or "").splitlines()
    cleaned = []
    skip_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped.lstrip("#").strip().lower()
            skip_section = any(term in title for term in NOISE_SECTION_TITLES)
            if skip_section:
                continue
        elif stripped.startswith("# "):
            skip_section = False
        if skip_section:
            continue
        if is_noise_line(line):
            continue
        cleaned.append(line)

    compact = []
    blank_count = 0
    for line in cleaned:
        if line.strip():
            blank_count = 0
            compact.append(line.rstrip())
        else:
            blank_count += 1
            if blank_count <= 1:
                compact.append("")
    return "\n".join(compact).strip() + "\n"


def creative_revision_note(value):
    text = str(value or "").strip()
    if not text or is_noise_line(text):
        return ""
    return text


def analyze_reference_images(refs):
    if not refs:
        return []
    endpoint = os.environ.get(
        "AD_MATERIAL_CODEX_VISION_URL",
        "http://127.0.0.1:18796/api/ad-material-vision/analyze",
    ).strip()
    if not endpoint:
        return []
    prompt = (
        "Return strict JSON only. Analyze each ad image reference. For each item "
        "provide id, layout, colors, main_text, visual_elements, transferable_points, "
        "avoid_copying, and production_notes in Chinese."
    )
    payload = {
        "prompt": prompt,
        "refs": [
            {
                "id": ref["code"],
                "name": ref["name"],
                "archive_url": ref["url"],
                "url": ref["url"],
            }
            for ref in refs
            if ref.get("url")
        ],
    }
    if not payload["refs"]:
        return []
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=int(os.environ.get("AD_MATERIAL_VISION_TIMEOUT", "180"))) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    try:
        data = json.loads(body)
    except Exception:
        return []
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def build_local_reference_demand(task, size_plan, revision_instruction="", reference_analysis=None):
    product = str(task.get("product_name") or task.get("app_id") or "未命名产品").strip()
    task_id = str(task.get("task_id") or "").strip()
    country = str(task.get("country") or "").strip()
    language = str(task.get("language") or "").strip()
    description = str(task.get("description") or "").strip()
    kind = task_kind(task)
    refs = reference_items(task)
    analysis_items = reference_analysis or []
    analysis_by_code = {}
    for index, item in enumerate(analysis_items, 1):
        key = str(item.get("id") or "").strip()
        if not key or key.lower().startswith("image #"):
            key = "REF_%02d" % index
        analysis_by_code[key] = item
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
        "## 制作信息",
        "",
        "- 产品：%s" % product,
        "- 国家/语言：%s / %s" % (country or "未填写", language or "未填写"),
        "- 尺寸与数量：%s" % (size_summary or "未填写"),
        "- 输出数量：%s 张静态图片" % quantity,
        "- 用户需求描述：%s" % (description or "未填写；默认按上传参考素材做风格迁移与同类新图。"),
    ]
    if title or body or category or tag_name:
        lines.extend([
            "- title：%s" % title,
            "- body：%s" % body,
        ])
    if store_url or product_icon_url:
        lines.extend(["", "## 产品基础信息", ""])
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
            "以下参考素材是本需求的核心输入。生成需求和后续生图必须先解析这些图的版式、色彩、主体、金额/利益点层级、CTA 位置和免责声明位置，再迁移到当前产品与目标语言。",
            "",
            "| 编号 | 上传文件 | 预览 | 需要继承 | 必须规避 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for ref in refs:
            preview = '<img src="%s" width="180">' % ref["url"] if ref["url"] else ""
            analysis = analysis_by_code.get(ref["code"], {})
            inherit = str(analysis.get("transferable_points") or "").strip()
            avoid = str(analysis.get("avoid_copying") or "").strip()
            if not inherit:
                inherit = "构图比例、主标题层级、金额/利益点大字、卡片式信息区、CTA 按钮位置、蓝白金融视觉节奏。"
            if not avoid:
                avoid = "不得照搬原图品牌、金额、币种、承诺、Logo、合规标识或原图中的不可验证还款数据。"
            lines.append("| %s | %s | %s | %s | %s |" % (ref["code"], ref["name"], preview, inherit, avoid))
    else:
        lines.extend([
            "- 未检测到上传参考素材；请在任务描述中明确画面结构、主视觉、文案和品牌规范。",
        ])

    if analysis_items:
        lines.extend(["", "## 参考素材视觉拆解", ""])
        for index, ref in enumerate(refs, 1):
            analysis = analysis_by_code.get(ref["code"], {})
            if not analysis:
                continue
            lines.extend([
                "### %s：%s" % (ref["code"], ref["name"]),
                "",
                "- 构图：%s" % (analysis.get("layout") or "未返回"),
                "- 色彩：%s" % (analysis.get("colors") or "未返回"),
                "- 可见主文案：%s" % (analysis.get("main_text") or "未返回"),
                "- 视觉元素：%s" % (analysis.get("visual_elements") or "未返回"),
                "- 可迁移点：%s" % (analysis.get("transferable_points") or "未返回"),
                "- 禁止照搬：%s" % (analysis.get("avoid_copying") or "未返回"),
                "- 制作提醒：%s" % (analysis.get("production_notes") or "未返回"),
                "",
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
            "- 验收标准：尺寸正确；能看出来自上传参考素材的风格衍生；没有乱码文字、没有未验证金融承诺。",
            "",
        ])

    lines.extend([
        "## 禁止项",
        "",
        "- 禁止出现参考图原品牌、竞品 Logo、二维码、商店按钮、真实证件、银行卡、OTP、联系人或催收压力表达。",
        "- 禁止承诺秒批、必过、免审、立即到账、固定月供、固定总还款额，除非用户或产品资料明确提供。",
        "- 禁止把参考图简单换色、换字后直接交付；必须做当前产品的新构图或新组合。",
    ])
    revision_note = creative_revision_note(revision_instruction)
    if revision_note:
        lines.extend(["", "## 制作调整方向", "", revision_note])
    return clean_material_demand_text("\n".join(lines))


def build_and_write_local_reference_output(task, payload, size_plan, output_path):
    revision_instruction = str((payload.get("extra") or {}).get("reason") or task.get("review_reason") or "").strip()
    refs = reference_items(task)
    reference_analysis = analyze_reference_images(refs)
    demand_text = build_local_reference_demand(task, size_plan, revision_instruction, reference_analysis)
    task_id = str(task.get("task_id") or os.environ.get("AD_MATERIAL_TASK_ID") or "ad_material").strip()
    markdown_path, markdown_url, evidence_path = local_reference_artifacts(task_id)
    markdown_path.write_text(demand_text, encoding="utf-8")
    evidence = {
        "provider": "local_reference",
        "task_kind": task_kind(task),
        "competitor_source": task.get("competitor_source") or "",
        "reference_files": refs,
        "reference_analysis": reference_analysis,
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


def build_local_iteration_demand(task, size_plan, internal_refs, reference_analysis=None, revision_instruction=""):
    product = str(task.get("product_name") or task.get("app_id") or "未命名产品").strip()
    country = str(task.get("country") or "").strip()
    language = str(task.get("language") or "").strip()
    description = str(task.get("description") or "").strip()
    slots = output_slots(size_plan)
    quantity = len(slots) or max(1, int(task.get("quantity") or 1))
    size_summary = ", ".join("%s x %s" % (item.get("size"), item.get("count")) for item in size_plan) or str(task.get("size") or "")
    product_icon_url = str(task.get("product_icon_url") or "").strip()
    analysis_items = reference_analysis or []
    analysis_by_code = {}
    for index, item in enumerate(analysis_items, 1):
        key = str(item.get("id") or "").strip()
        if not key or key.lower().startswith("image #"):
            key = "INT_REF_%02d" % index
        analysis_by_code[key] = item

    lines = [
        "# %s 投放素材需求" % product,
        "",
        "## 制作信息",
        "",
        "- 产品：%s" % product,
        "- 国家/语言：%s / %s" % (country or "未填写", language or "未填写"),
        "- 尺寸与数量：%s" % (size_summary or "未填写"),
        "- 输出数量：%s 张静态图片" % quantity,
        "- 用户需求描述：%s" % (description or "基于历史优质静态素材做同类迭代。"),
    ]
    if product_icon_url:
        lines.extend([
            "",
            "## 产品基础信息",
            "",
            "- 产品图标：%s" % product_icon_url,
            "",
            '<img src="%s" width="96">' % product_icon_url,
        ])

    lines.extend(["", "## 历史优质素材参考", ""])
    if internal_refs:
        lines.extend([
            "以下老素材来自当前产品历史静态素材表现较好的样本。新图应学习它们的构图、信息层级、色彩节奏、CTA 位置和卖点呈现方式，再做新的组合与表达。",
            "",
            "| 编号 | 预览 | 可学习点 | 必须规避 |",
            "| --- | --- | --- | --- |",
        ])
        for index, ref in enumerate(internal_refs, 1):
            code = str(ref.get("asset_id") or "INT_REF_%02d" % index)
            url = str(ref.get("archive_url") or "").strip()
            preview = '<img src="%s" width="180">' % url if url else ""
            analysis = analysis_by_code.get(code, {})
            learning = compact_text(
                analysis.get("transferable_points")
                or ref.get("learning_point")
                or "学习该素材的主视觉结构、卖点层级、色彩节奏和 CTA 位置。",
                220,
            )
            avoid = compact_text(
                analysis.get("avoid_copying")
                or "不要原样复制旧素材的具体金额、人物、背景、Logo 位置和排版细节；需要做新的视觉组合。",
                220,
            )
            lines.append("| %s | %s | %s | %s |" % (code, preview, learning, avoid))
    else:
        lines.append("- 未检索到可用的历史优质静态素材。制作时请按用户描述和产品信息补足清晰画面方向。")

    if analysis_items:
        lines.extend(["", "## 历史素材视觉拆解", ""])
        for index, ref in enumerate(internal_refs, 1):
            code = str(ref.get("asset_id") or "INT_REF_%02d" % index)
            analysis = analysis_by_code.get(code, {})
            if not analysis:
                continue
            lines.extend([
                "### %s" % code,
                "",
                "- 构图：%s" % (analysis.get("layout") or "未返回"),
                "- 色彩：%s" % (analysis.get("colors") or "未返回"),
                "- 可见主文案：%s" % (analysis.get("main_text") or "未返回"),
                "- 视觉元素：%s" % (analysis.get("visual_elements") or "未返回"),
                "- 可迁移点：%s" % (analysis.get("transferable_points") or "未返回"),
                "- 禁止照搬：%s" % (analysis.get("avoid_copying") or "未返回"),
                "- 制作提醒：%s" % (analysis.get("production_notes") or "未返回"),
                "",
            ])

    ref_codes = ", ".join(str(ref.get("asset_id") or "INT_REF_%02d" % (idx + 1)) for idx, ref in enumerate(internal_refs)) or "历史优质素材"
    lines.extend([
        "",
        "## 迭代制作要求",
        "",
        "- 先分析历史优质素材的共同点，再进行新构图；不要只做换色、换字或简单替换 Logo。",
        "- 保留高转化结构：强主标题、清晰金额/利益点、卡片式信息区、明确 CTA、底部免责声明安全区。",
        "- 语言、货币、产品名、额度、周期和合规表述必须替换为当前产品与当前市场可用信息。",
        "- Logo 必须使用当前产品 Logo 或预留后置叠加位置；不得重新绘制近似 Logo。",
        "",
        "## 逐张素材需求",
        "",
    ])

    for slot in slots or [{"index": i, "size": str(task.get("size") or "1:1"), "dimensions": normalize_size(task.get("size"))} for i in range(1, quantity + 1)]:
        index = int(slot["index"])
        size = slot["size"]
        dimensions = slot["dimensions"]
        primary = str(internal_refs[(index - 1) % len(internal_refs)].get("asset_id")) if internal_refs else ref_codes
        if index % 2 == 1:
            angle = "继承老素材的大标题 + 核心利益点 + CTA 结构，重做为当前产品的新视觉。"
            focus = "首屏冲击和点击引导。"
        else:
            angle = "继承老素材的信息卡片/额度展示方式，重做更清晰的产品解释型素材。"
            focus = "透明信息和可信表达。"
        lines.extend([
            "### 素材 %02d" % index,
            "",
            "- 输出规格：%s（%s）" % (size, dimensions),
            "- 主参考老素材：%s" % primary,
            "- 需求目标：%s" % (description or "基于历史优质素材做同类迭代，产出当前产品可用的新静态图。"),
            "- 构图方向：%s" % angle,
            "- 表达重点：%s" % focus,
            "- 画面要求：延续历史优质素材的清晰层级和金融视觉识别，但必须更换画面组合、文案、图标和品牌元素。",
            "- 文案要求：使用 %s 语言；主标题和 CTA 必须完整可读；不要照搬旧素材中的具体金额、周期或承诺。" % (language or "目标市场"),
            "- 验收标准：尺寸正确；能看出来自历史优质素材的结构继承；没有乱码文字、没有未验证金融承诺、没有旧素材品牌残留。",
            "",
        ])

    lines.extend([
        "## 禁止项",
        "",
        "- 禁止直接复刻旧素材；必须做新构图或新组合。",
        "- 禁止出现旧素材中的其他产品名、旧 Logo、水印、二维码、商店按钮、真实证件、银行卡、OTP、联系人或催收压力表达。",
        "- 禁止承诺秒批、必过、免审、立即到账、固定月供、固定总还款额，除非用户或产品资料明确提供。",
    ])
    revision_note = creative_revision_note(revision_instruction)
    if revision_note:
        lines.extend(["", "## 制作调整方向", "", revision_note])
    return clean_material_demand_text("\n".join(lines))


def build_and_write_local_iteration_output(task, payload, size_plan, output_path):
    revision_instruction = str((payload.get("extra") or {}).get("reason") or task.get("review_reason") or "").strip()
    task_id = str(task.get("task_id") or os.environ.get("AD_MATERIAL_TASK_ID") or "ad_material").strip()
    internal_refs, internal_evidence = collect_internal_winners(task, limit=int(os.environ.get("AD_MATERIAL_ITERATION_INTERNAL_REF_LIMIT", "6")))
    visual_refs = [internal_ref_to_visual_ref(item) for item in internal_refs]
    reference_analysis = analyze_reference_images(visual_refs)
    demand_text = build_local_iteration_demand(task, size_plan, internal_refs, reference_analysis, revision_instruction)
    markdown_path, markdown_url, evidence_path = local_task_artifacts(task_id, "iteration")
    markdown_path.write_text(demand_text, encoding="utf-8")
    evidence = {
        "provider": "local_iteration",
        "task_kind": "iteration",
        "internal_evidence": internal_evidence,
        "internal_refs": internal_refs,
        "reference_analysis": reference_analysis,
        "size_plan": size_plan,
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    write_output(output_path, {
        "demand_text": demand_text,
        "markdown": demand_text,
        "provider": "local_iteration",
        "artifacts": {
            "provider": "local_iteration",
            "markdown_path": str(markdown_path),
            "markdown_url": markdown_url,
            "evidence_path": str(evidence_path),
            "internal_refs": str(len(internal_refs)),
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
    if provider == "local_iteration":
        build_and_write_local_iteration_output(task, payload, size_plan, output_path)
        return
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

    demand_text = clean_material_demand_text(append_size_plan(Path(markdown_path).read_text(encoding="utf-8"), size_plan))
    write_output(output_path, {
        "demand_text": demand_text,
        "markdown": demand_text,
        "provider": provider,
        "artifacts": result,
    })


if __name__ == "__main__":
    main()
