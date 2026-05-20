#!/usr/bin/env python3
import json
import html
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


def compact_analysis_value(value, limit=220):
    if isinstance(value, (list, tuple)):
        parts = [compact_text(item, 0).rstrip("。；;") for item in value if compact_text(item, 0)]
        text = "；".join(parts)
    else:
        text = compact_text(value, 0)
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def compact_store_value(value, limit=600):
    text = "" if value is None else str(value)
    text = text.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\n", " ").replace("\\/", "/")
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def unique_values(values, limit=8):
    out = []
    seen = set()
    for value in values or []:
        text = compact_store_value(value, limit=420)
        key = text.lower()
        if text and key not in seen:
            out.append(text)
            seen.add(key)
        if len(out) >= limit:
            break
    return out


def sentence_hits(text, patterns, limit=6):
    chunks = re.split(r"[。.!?；;]\s*|\n+", str(text or ""))
    hits = []
    for chunk in chunks:
        clean = compact_store_value(chunk, limit=420)
        if not clean:
            continue
        if any(re.search(pattern, clean, flags=re.I) for pattern in patterns):
            hits.append(clean)
        if len(hits) >= limit:
            break
    return unique_values(hits, limit=limit)


def extract_store_financial_facts(text):
    clean = compact_store_value(text, limit=7000)
    direct_amounts = []
    for pattern in [
        r"(?:Variasi\s+jumlah\s+pinjaman\s+uang|jumlah\s+pinjaman|limit|plafon|monto|amount)[^:：]{0,30}[:：]\s*((?:IDR|Rp\.?|MXN|USD|\$|₱|₹)\s?[\d.,]+(?:\s*[-–]\s*(?:IDR|Rp\.?|MXN|USD|\$|₱|₹)?\s?[\d.,]+)?)",
        r"((?:IDR|Rp\.?|MXN|USD|\$|₱|₹)\s?[\d.,]+\s*[-–]\s*(?:IDR|Rp\.?|MXN|USD|\$|₱|₹)?\s?[\d.,]+)",
    ]:
        for match in re.finditer(pattern, clean, flags=re.I):
            direct_amounts.append(match.group(1))
    money = unique_values(
        direct_amounts + re.findall(
            r"(?:hingga|sampai|mulai|limit|plafon|jumlah|pinjaman|amount|monto|cr[eé]dito|pr[eé]stamo|loan)?[^。.!?;\n]{0,50}(?:Rp\.?|IDR|RM|MXN|USD|COP|BRL|PHP|INR|\$|₱|₹)\s?[\d.,]+(?:\s*[-–]\s*(?:Rp\.?|IDR|RM|MXN|USD|COP|BRL|PHP|INR|\$|₱|₹)?\s?[\d.,]+)?[^。.!?;\n]{0,30}",
            clean,
            flags=re.I,
        ),
        limit=8,
    )
    direct_terms = []
    for pattern in [
        r"(?:Variasi\s+tenor\s+pinjaman\s+uang|tenor|jangka\s+waktu|plazo|term|period)[^:：]{0,30}[:：]?\s*(\d+\s*[-–]\s*\d+\s*(?:hari|bulan|minggu|tahun|d[ií]as|days|months|weeks))",
    ]:
        for match in re.finditer(pattern, clean, flags=re.I):
            direct_terms.append(match.group(1))
    terms = unique_values(
        direct_terms + sentence_hits(
            clean,
            [
                r"\b\d+\s*(?:a|-|to|sampai|hingga)\s*\d+\s*(?:hari|bulan|minggu|tahun|d[ií]as|days|meses|months|weeks)\b",
                r"\b\d+\s*(?:hari|bulan|minggu|tahun|d[ií]as|days|meses|months|weeks)\b",
                r"\b(?:tenor|jangka waktu|periode|plazo|term|period|cuotas?)\b",
            ],
            limit=8,
        ),
        limit=8,
    )
    direct_rates = []
    for pattern in [
        r"(?:Bunga\s+rendah\s*\(maksimum\)|bunga[^:：]{0,40}maksimum|suku\s+bunga|interest|rate|fee)[^:：]{0,40}[:：]\s*(\d+(?:[.,]\d+)?\s?%\s*(?:per\s*tahun|pertahun|per\s*bulan|bulanan|monthly|annual|APR|CAT)?)",
        r"(\d+(?:[.,]\d+)?\s?%\s*(?:per\s*tahun|pertahun|per\s*bulan|bulanan|monthly|annual|APR|CAT))",
    ]:
        for match in re.finditer(pattern, clean, flags=re.I):
            direct_rates.append(match.group(1))
    rates = unique_values(
        direct_rates + sentence_hits(
            clean,
            [
                r"\b(?:bunga|suku bunga|biaya|admin|layanan|interest|rate|fee|CAT|APR|TEA|TCEA|tasa|inter[eé]s|comisi[oó]n|commission)\b[^。.!?;]{0,120}\d+(?:[.,]\d+)?\s?%",
                r"\d+(?:[.,]\d+)?\s?%[^。.!?;]{0,120}\b(?:bunga|suku bunga|biaya|admin|layanan|interest|rate|fee|CAT|APR|TEA|TCEA|tasa|inter[eé]s|comisi[oó]n|commission)\b",
            ],
            limit=8,
        ),
        limit=8,
    )
    repayment = sentence_hits(
        clean,
        [
            r"\b(?:angsuran|cicilan|pembayaran|bayar|pelunasan|tagihan|repayment|payment|pago|cuota|installment)\b",
        ],
        limit=6,
    )
    requirements = sentence_hits(
        clean,
        [
            r"\b(?:syarat|persyaratan|KTP|WNI|usia|umur|rekening bank|rekening|bank account|income|pendapatan|requisito|requirements?|INE|CURP|RFC|edad|age|identificaci[oó]n)\b",
        ],
        limit=6,
    )
    compliance = sentence_hits(
        clean,
        [
            r"\b(?:OJK|AFPI|berizin|terdaftar|diawasi|otoritas jasa keuangan|privacy|keamanan|regulated|supervised|authorized|license|licence)\b",
        ],
        limit=6,
    )
    finance_keywords = [
        "pinjaman", "kredit", "dana", "uang", "tunai", "tenor", "cicilan", "angsuran",
        "prestamo", "préstamo", "credito", "crédito", "loan", "cash", "finanzas",
        "plazo", "tasa", "interés", "interest", "repayment", "pago", "cat", "apr",
    ]
    return {
        "is_financial_product": any(keyword in clean.lower() for keyword in finance_keywords) or bool(money or rates or terms),
        "amount_or_limit_claims": money,
        "term_or_period_claims": terms,
        "interest_fee_or_rate_claims": rates,
        "repayment_claims": repayment,
        "eligibility_or_requirement_claims": requirements,
        "trust_or_compliance_claims": compliance,
        "usage_rule": "Only use facts explicitly present in the store text. Do not invent limits, periods, interest, fees, approval speed, or eligibility.",
    }


def merge_store_facts(primary, secondary):
    primary = primary if isinstance(primary, dict) else {}
    secondary = secondary if isinstance(secondary, dict) else {}
    keys = [
        "amount_or_limit_claims",
        "term_or_period_claims",
        "interest_fee_or_rate_claims",
        "repayment_claims",
        "eligibility_or_requirement_claims",
        "trust_or_compliance_claims",
    ]
    merged = dict(primary)
    for key in keys:
        merged[key] = unique_values(list(primary.get(key) or []) + list(secondary.get(key) or []), limit=8)
    merged["is_financial_product"] = bool(primary.get("is_financial_product") or secondary.get("is_financial_product"))
    merged["usage_rule"] = primary.get("usage_rule") or secondary.get("usage_rule") or "Only use explicitly provided product facts."
    return merged


def empty_store_profile(task, status="missing_store_url", note=""):
    notes = [note] if note else []
    return {
        "source": "app_store",
        "store_url": str(task.get("store_url") or "").strip(),
        "package": str(task.get("package_name") or task.get("package") or "").strip(),
        "status": status,
        "title": "",
        "developer": "",
        "description_excerpt": "",
        "icon_url": str(task.get("product_icon_url") or "").strip(),
        "image_urls": [],
        "financial_facts": extract_store_financial_facts(""),
        "notes": notes,
    }


def normalize_store_profile(profile, task):
    profile = dict(profile or empty_store_profile(task))
    profile["store_url"] = str(profile.get("store_url") or task.get("store_url") or "").strip()
    profile["package"] = str(profile.get("package") or task.get("package_name") or task.get("package") or "").strip()
    profile["title"] = compact_store_value(profile.get("title"), limit=240)
    profile["developer"] = compact_store_value(profile.get("developer"), limit=180)
    profile["description_excerpt"] = compact_store_value(profile.get("description_excerpt"), limit=3000)
    profile["icon_url"] = str(profile.get("icon_url") or task.get("product_icon_url") or "").strip()
    helper_facts = profile.get("financial_facts") if isinstance(profile.get("financial_facts"), dict) else {}
    local_facts = extract_store_financial_facts(profile.get("description_excerpt") or "")
    profile["financial_facts"] = merge_store_facts(local_facts, helper_facts)
    if not isinstance(profile.get("notes"), list):
        profile["notes"] = [str(profile.get("notes"))] if profile.get("notes") else []
    return profile


def fetch_store_profile(task):
    store_url = str(task.get("store_url") or "").strip()
    if not store_url:
        return empty_store_profile(task)
    app_info = {
        "store_url": store_url,
        "package": str(task.get("package_name") or task.get("package") or "").strip(),
        "package_name": str(task.get("package_name") or task.get("package") or "").strip(),
    }
    try:
        module = load_skill_module("guangdada")
        fetcher = getattr(module, "fetch_store_product_profile", None)
        if callable(fetcher):
            return normalize_store_profile(fetcher(app_info), task)
    except Exception as exc:
        fallback = empty_store_profile(task, status="fetch_failed", note=str(exc)[:180])
        return fallback
    return empty_store_profile(task, status="fetcher_missing")


def store_fact_values(store_profile, key, limit=4):
    facts = (store_profile or {}).get("financial_facts") or {}
    values = []
    for item in facts.get(key) or []:
        text = compact_text(item, 180)
        if not text:
            continue
        lower = text.lower()
        if any(token in text for token in ("◉", "👉")) and key in {"amount_or_limit_claims", "term_or_period_claims", "interest_fee_or_rate_claims"}:
            continue
        if key in {"amount_or_limit_claims", "term_or_period_claims", "interest_fee_or_rate_claims"} and len(text) > 140:
            continue
        if key in {"amount_or_limit_claims", "interest_fee_or_rate_claims", "repayment_claims"} and re.match(r"^\d{2,}\b", text):
            continue
        if key == "amount_or_limit_claims" and not re.search(r"(?:IDR|Rp\.?|MXN|USD|\$|₱|₹)\s?[\d.,]+", text, flags=re.I):
            continue
        if key == "amount_or_limit_claims" and any(token in lower for token in ("contoh", "bunga", "suku bunga", "%")):
            continue
        if key == "term_or_period_claims" and not re.search(r"\d+[\s\-–]*(?:a|to|sampai|hingga)?\s*\d*\s*(?:hari|bulan|minggu|tahun|d[ií]as|days|months|weeks)|tenor|jangka waktu|plazo|term", text, flags=re.I):
            continue
        if key == "term_or_period_claims" and any(token in lower for token in ("rp", "idr", "%", "wni", "ktp", "bunga")):
            continue
        if key == "interest_fee_or_rate_claims" and "%" not in text:
            continue
        if key == "interest_fee_or_rate_claims" and any(token in lower for token in ("rp", "idr", "contoh", "tenor")):
            continue
        if key == "repayment_claims" and (len(text) > 140 or "online loan" in lower or "pinjaman uang tunai" in lower):
            continue
        if key == "eligibility_or_requirement_claims" and text.startswith("200 Syarat"):
            continue
        if text not in values:
            values.append(text)
        if len(values) >= limit:
            break
    return values


def store_fact_summary(store_profile):
    parts = []
    for label, key in [
        ("额度/金额", "amount_or_limit_claims"),
        ("期限/周期", "term_or_period_claims"),
        ("利息/费用", "interest_fee_or_rate_claims"),
        ("申请条件", "eligibility_or_requirement_claims"),
        ("合规/可信", "trust_or_compliance_claims"),
    ]:
        values = store_fact_values(store_profile, key, limit=3)
        if values:
            parts.append("%s：%s" % (label, "；".join(values)))
    return "；".join(parts)


def product_basic_info_lines(task, store_profile=None):
    store_profile = normalize_store_profile(store_profile, task) if store_profile else empty_store_profile(task)
    lines = []
    store_url = str(task.get("store_url") or store_profile.get("store_url") or "").strip()
    package_name = str(task.get("package_name") or store_profile.get("package") or "").strip()
    product_icon_url = str(task.get("product_icon_url") or store_profile.get("icon_url") or "").strip()
    if store_url:
        lines.append("- 商店链接：%s" % store_url)
    if package_name:
        lines.append("- 包名：%s" % package_name)
    if store_profile.get("title"):
        lines.append("- 商店标题：%s" % store_profile.get("title"))
    if store_profile.get("developer"):
        lines.append("- 开发者：%s" % store_profile.get("developer"))
    if product_icon_url:
        lines.append("- 产品图标：%s" % product_icon_url)
        lines.append("")
        lines.append('<img src="%s" width="96">' % product_icon_url)
    facts = store_profile.get("financial_facts") or {}
    for label, key in [
        ("额度/金额", "amount_or_limit_claims"),
        ("期限/周期", "term_or_period_claims"),
        ("利息/费用", "interest_fee_or_rate_claims"),
        ("还款/费用透明", "repayment_claims"),
        ("申请条件/资质", "eligibility_or_requirement_claims"),
        ("合规/可信", "trust_or_compliance_claims"),
    ]:
        values = store_fact_values(store_profile, key, limit=4)
        if values:
            lines.append("- %s：%s" % (label, "；".join(values)))
    if facts.get("is_financial_product"):
        lines.append("- 产品事实使用规则：图片文案只能使用上面商店描述明示的额度、期限、费率、还款、申请条件和合规事实；不得编造秒批、必过、固定到账、固定月供或未验证金额。")
    elif store_url:
        lines.append("- 产品事实使用规则：商店描述未提取到明确金融参数时，不得在图片中编造额度、利率、期限、审批速度或资质。")
    return [line for line in lines if line is not None]


def product_copy_constraint_lines(task, store_profile=None):
    product = str(task.get("product_name") or task.get("app_id") or "当前产品").strip()
    language = str(task.get("language") or "目标语言").strip()
    summary = store_fact_summary(store_profile or {})
    lines = [
        "",
        "## 产品文案事实约束",
        "",
        "- 图片中的主标题、副文案、按钮和小字必须服务于 %s 的产品卖点；不得只写含义泛泛或与贷款弱相关的口号。" % product,
        "- 文案语言使用 %s；若画面包含具体金额、期限、费率、资质或申请条件，必须来自“产品基础信息”中的商店明示事实。" % (language or "目标语言"),
    ]
    if summary:
        lines.append("- 可用产品参数：%s" % summary)
        lines.append("- 每张图至少绑定 1 个可用产品参数或明确的申请/费用透明场景，例如额度、期限、费率、申请条件、费用明细或合规资质。")
    else:
        lines.append("- 当前没有可验证产品参数时，禁止写具体额度、期限、费率和到账承诺；只能用“在应用内查看额度/期限/费用”等保守表达。")
    if language.lower().startswith("id"):
        lines.append("- 印尼语文案方向优先围绕 `pinjaman online`、`limit`、`tenor`、`biaya/bunga`、`Ajukan Sekarang`；避免只写 `Pembayaran lebih jelas` 这类不说明贷款产品参数的泛文案。")
    lines.append("- 生成图片时禁止出现乱码、混合语言、无意义短语、与产品无关的生活口号，禁止把参考图中的竞品文案直接搬到当前产品。")
    return lines


def build_analysis_map(refs, analysis_items, fallback_prefix):
    mapping = {}
    refs = refs or []
    for index, item in enumerate(analysis_items or [], 1):
        if not isinstance(item, dict):
            continue
        keys = []
        raw_id = str(item.get("id") or "").strip()
        if raw_id:
            keys.append(raw_id)
            lower = raw_id.lower()
            match = re.search(r"(\d+)$", lower)
            if match:
                number = int(match.group(1))
                keys.append("%s_%02d" % (fallback_prefix, number))
                keys.append("%s_%d" % (fallback_prefix, number))
                if number <= len(refs):
                    code = str(refs[number - 1].get("code") or "").strip()
                    if code:
                        keys.append(code)
        if index <= len(refs):
            code = str(refs[index - 1].get("code") or "").strip()
            if code:
                keys.append(code)
        keys.append("%s_%02d" % (fallback_prefix, index))
        for key in keys:
            if key and key not in mapping:
                mapping[key] = item
    return mapping


def reference_style_fields(analysis):
    if not isinstance(analysis, dict) or not analysis:
        return "", "", ""
    layout = compact_analysis_value(analysis.get("layout"), 260)
    colors = compact_analysis_value(analysis.get("colors"), 220)
    elements = compact_analysis_value(analysis.get("visual_elements"), 260)
    transfer = compact_analysis_value(analysis.get("transferable_points"), 260)
    main_text = compact_analysis_value(analysis.get("main_text"), 220)
    angle_parts = []
    if layout:
        angle_parts.append("构图骨架：" + layout)
    if elements:
        angle_parts.append("视觉元素：" + elements)
    focus_parts = []
    if transfer:
        focus_parts.append("可迁移点：" + transfer)
    if main_text:
        focus_parts.append("原图信息层级：" + main_text)
    visual_parts = []
    if colors:
        visual_parts.append("色彩节奏：" + colors)
    if elements:
        visual_parts.append("主体/装饰：" + elements)
    return "；".join(angle_parts), "；".join(focus_parts), "；".join(visual_parts)


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


WATERMARK_HARD_CONSTRAINT_MARKER = "生成的素材中不含任何水印元素"
WATERMARK_HARD_CONSTRAINT_TEXT = (
    "- 水印隔离：清除参考素材/参数素材中水印的影响；参考素材里的斜向水印、"
    "半透明品牌字样、版权标记或平台水印只用于识别来源，生成的素材中不含任何水印元素，"
    "不得出现原水印文字、形状、角度、透明叠层或残影，也不得把水印当作背景纹理或装饰。"
)
FORMAT_ALIGNMENT_HARD_CONSTRAINT_MARKER = "文案和背景容器必须严格对齐"
FORMAT_ALIGNMENT_HARD_CONSTRAINT_TEXT = (
    "- 版式对齐：文案和背景容器必须严格对齐；所有主标题、副文案、信息卡、表格字段、"
    "按钮文字、标签和免责声明必须完整落在对应白底/色块/卡片/表格/按钮内部，"
    "不得跨出边框、压线、悬浮在背景外、贴住容器边缘或与装饰元素重叠。"
    "若文字过长，必须缩短文案、换行、缩小字号或放大容器；禁止让文字溢出表格、卡片或按钮。"
)


def ensure_global_hard_constraints(text):
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    missing = []
    if WATERMARK_HARD_CONSTRAINT_MARKER not in normalized:
        missing.append(WATERMARK_HARD_CONSTRAINT_TEXT)
    if FORMAT_ALIGNMENT_HARD_CONSTRAINT_MARKER not in normalized:
        missing.append(FORMAT_ALIGNMENT_HARD_CONSTRAINT_TEXT)
    if not missing:
        return normalized + "\n"
    if "## 全局硬性约束" in normalized:
        return normalized + "\n" + "\n".join(missing) + "\n"
    return normalized + "\n\n## 全局硬性约束\n\n" + "\n".join(missing) + "\n"


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
    return ensure_global_hard_constraints("\n".join(compact))


def creative_revision_note(value):
    text = str(value or "").strip()
    if not text or is_noise_line(text):
        return ""
    meaningful = re.sub(r"[\s?？!！.。,:：;；_\-]+", "", text)
    if not meaningful:
        return ""
    if text.count("?") + text.count("？") >= max(6, len(text) * 0.45):
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
        "Return strict JSON only. Analyze each ad image reference. Use the input ref id "
        "exactly as the output id. For each item provide id, layout, colors, main_text, "
        "visual_elements, transferable_points, avoid_copying, and production_notes in Chinese. "
        "The transferable_points and avoid_copying must be specific to that image; do not use "
        "the same generic text for every image."
    )
    ref_payloads = [
        {
            "id": ref["code"],
            "name": ref["name"],
            "archive_url": ref["url"],
            "url": ref["url"],
        }
        for ref in refs
        if ref.get("url")
    ]
    if not ref_payloads:
        return []
    timeout = int(os.environ.get("AD_MATERIAL_VISION_TIMEOUT", "360"))
    batch_size = max(1, int(os.environ.get("AD_MATERIAL_VISION_BATCH_SIZE", "2")))
    analysis = []
    for offset in range(0, len(ref_payloads), batch_size):
        batch = ref_payloads[offset : offset + batch_size]
        request = urllib.request.Request(
            endpoint,
            data=json.dumps({"prompt": prompt, "refs": batch}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            continue
        result = data.get("result") if isinstance(data, dict) else None
        if isinstance(result, list):
            analysis.extend(item for item in result if isinstance(item, dict))
    return analysis


def build_local_reference_demand(task, size_plan, revision_instruction="", reference_analysis=None, store_profile=None):
    product = str(task.get("product_name") or task.get("app_id") or "未命名产品").strip()
    task_id = str(task.get("task_id") or "").strip()
    country = str(task.get("country") or "").strip()
    language = str(task.get("language") or "").strip()
    description = str(task.get("description") or "").strip()
    kind = task_kind(task)
    refs = reference_items(task)
    analysis_items = reference_analysis or []
    analysis_by_code = build_analysis_map(refs, analysis_items, "REF")
    slots = output_slots(size_plan)
    quantity = len(slots) or max(1, int(task.get("quantity") or 1))
    size_summary = ", ".join("%s x %s" % (item.get("size"), item.get("count")) for item in size_plan) or str(task.get("size") or "")
    title = str(task.get("title") or "").strip()
    body = str(task.get("body") or "").strip()
    tag_name = str(task.get("tag_name") or "").strip()
    category = str(task.get("category") or "").strip()
    store_profile = normalize_store_profile(store_profile, task) if store_profile else empty_store_profile(task)

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
    basic_lines = product_basic_info_lines(task, store_profile)
    if basic_lines:
        lines.extend(["", "## 产品基础信息", ""])
        lines.extend(basic_lines)
    lines.extend(product_copy_constraint_lines(task, store_profile))

    lines.extend([
        "",
        "## 参考素材",
        "",
    ])
    if refs:
        lines.extend([
            "以下参考素材是本需求的核心输入。生成需求和后续生图必须先解析这些图实际出现的版式、色彩、主体、金额/利益点层级、品牌区和免责声明位置，再迁移到当前产品与目标语言；不得用固定金融模板替代参考图。",
            "",
            "| 编号 | 上传文件 | 预览 | 需要继承 | 必须规避 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for ref in refs:
            preview = '<img src="%s" width="180">' % ref["url"] if ref["url"] else ""
            analysis = analysis_by_code.get(ref["code"], {})
            inherit = compact_analysis_value(analysis.get("transferable_points"), 260)
            avoid = compact_analysis_value(analysis.get("avoid_copying"), 260)
            if not inherit:
                inherit = "参考图实际构图比例、主体位置、主标题层级、核心数字/利益点层级、品牌区和底部免责声明安全区。"
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
                "- 视觉元素：%s" % (compact_analysis_value(analysis.get("visual_elements"), 0) or "未返回"),
                "- 可迁移点：%s" % (compact_analysis_value(analysis.get("transferable_points"), 0) or "未返回"),
                "- 禁止照搬：%s" % (compact_analysis_value(analysis.get("avoid_copying"), 0) or "未返回"),
                "- 制作提醒：%s" % (compact_analysis_value(analysis.get("production_notes"), 0) or "未返回"),
                "",
            ])

    lines.extend([
        "",
        "## 参考素材解析要求",
        "",
        "- 先看上传参考图，再写每张图的制作要求；不得用固定竞品模板替代参考图。",
        "- 视觉上优先保留参考图实际出现的高转化结构；参考图没有出现的手机、金币、功能卡、滑杆、箭头、强 CTA 不要默认添加。",
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
        primary_ref = refs[(index - 1) % len(refs)] if refs else None
        primary_ref_code = primary_ref["code"] if primary_ref else ref_codes
        primary_analysis = analysis_by_code.get(primary_ref_code, {}) if primary_ref else {}
        style_angle, style_focus, style_visual = reference_style_fields(primary_analysis)
        angle = style_angle or "严格以主参考素材的真实版式骨架为第一约束：先保留布局关系、主体位置和信息层级，再替换为当前产品内容。"
        focus = style_focus or "让用户一眼看出与上传参考素材同源的风格衍生，而不是通用贷款模板。"
        visual_rule = style_visual or "保留参考图实际存在的构图、色彩、主体位置、文字层级和免责声明安全区。"
        lines.extend([
            "### 素材 %02d" % index,
            "",
            "- 输出规格：%s（%s）" % (size, dimensions),
            "- 主参考素材：%s" % primary_ref_code,
            "- 需求目标：%s" % (description or "参考上传素材做同风格迭代，生成当前产品可用的新静态图。"),
            "- 构图方向：%s" % angle,
            "- 表达重点：%s" % focus,
            "- 画面要求：%s；必须替换为当前产品、当前国家和当前语言；不得额外套用参考图没有的手机、金币、功能卡、滑杆或箭头模板元素。" % visual_rule,
            "- 文案要求：使用 %s 语言；不得直接复制参考图的西语文案、MXN 币种、审批分钟数、具体还款金额或月供。" % (language or "目标市场"),
            "- 生成提示：把上传参考图和上方视觉拆解作为主约束，先复刻其版式骨架、信息层级和情绪/主体关系，再替换品牌、语言、产品事实和图标；图片中不出现竞品名、竞品 Logo 或竞品 UI。",
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
    store_profile = fetch_store_profile(task)
    demand_text = build_local_reference_demand(task, size_plan, revision_instruction, reference_analysis, store_profile)
    task_id = str(task.get("task_id") or os.environ.get("AD_MATERIAL_TASK_ID") or "ad_material").strip()
    markdown_path, markdown_url, evidence_path = local_reference_artifacts(task_id)
    markdown_path.write_text(demand_text, encoding="utf-8")
    evidence = {
        "provider": "local_reference",
        "task_kind": task_kind(task),
        "competitor_source": task.get("competitor_source") or "",
        "reference_files": refs,
        "reference_analysis": reference_analysis,
        "store_profile": store_profile,
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


def iteration_analysis_for_ref(analysis_by_code, ref, index):
    code = str(ref.get("asset_id") or "INT_REF_%02d" % index)
    return analysis_by_code.get(code, {}) if isinstance(analysis_by_code, dict) else {}


def pick_first_store_fact(store_profile, keys):
    for key in keys:
        values = store_fact_values(store_profile, key, limit=1)
        if values:
            return values[0]
    return ""


def short_visual_fact(value, fallback, limit=90):
    text = compact_text(value, 0)
    text = re.sub(r"[👉👇✅•]+", "", text).strip(" -；;")
    if not text or len(text) > limit:
        return fallback
    return text


def build_iteration_synthesis_lines(internal_refs, analysis_by_code):
    lines = [
        "",
        "## 优质素材综合拆解结论",
        "",
        "以下结论来自上方历史优质素材的共性提炼，只作为新创意方向输入；新素材不按某一张老素材一对一复刻。",
        "",
    ]
    if not internal_refs:
        lines.append("- 未检索到历史优质素材，后续创意只能按产品信息和用户描述生成。")
        return lines
    layout_points = []
    visual_points = []
    avoid_points = []
    for index, ref in enumerate(internal_refs, 1):
        analysis = iteration_analysis_for_ref(analysis_by_code, ref, index)
        code = str(ref.get("asset_id") or "INT_REF_%02d" % index)
        layout = compact_analysis_value(analysis.get("layout"), 120)
        transfer = compact_analysis_value(analysis.get("transferable_points"), 160)
        visual = compact_analysis_value(analysis.get("visual_elements") or analysis.get("colors"), 140)
        avoid = compact_analysis_value(analysis.get("avoid_copying"), 150)
        if layout:
            layout_points.append("%s：%s" % (code, layout))
        if transfer:
            visual_points.append("%s：%s" % (code, transfer))
        elif visual:
            visual_points.append("%s：%s" % (code, visual))
        if avoid:
            avoid_points.append("%s：%s" % (code, avoid))
    if layout_points:
        lines.append("- 可复用结构：%s" % "；".join(layout_points[:4]))
    if visual_points:
        lines.append("- 可吸收优点：%s" % "；".join(visual_points[:5]))
    if avoid_points:
        lines.append("- 必须规避的相似风险：%s" % "；".join(avoid_points[:4]))
    lines.extend([
        "- 本轮输出策略：从上述素材中抽取“信息清楚、金融可信、移动端可读、CTA 明确”的优点，重新组合成新的创意，不指定某一张老素材作为主模板。",
        "- 新图必须给出明确场景、版式、主文案方向和画面元素；禁止只写“根据老素材迭代”这类笼统要求。",
    ])
    return lines


def iteration_creative_blueprint(index, store_profile, language):
    amount = pick_first_store_fact(store_profile, ["amount_or_limit_claims"])
    term = pick_first_store_fact(store_profile, ["term_or_period_claims"])
    rate = pick_first_store_fact(store_profile, ["interest_fee_or_rate_claims"])
    eligibility = pick_first_store_fact(store_profile, ["eligibility_or_requirement_claims"])
    trust = pick_first_store_fact(store_profile, ["trust_or_compliance_claims"])
    amount_copy = short_visual_fact(amount, "在应用内查看可用额度", 80)
    term_copy = short_visual_fact(term, "按评估结果查看可用期限", 80)
    rate_copy = short_visual_fact(rate, "费用/利息以应用内展示为准", 80)
    eligibility_copy = short_visual_fact(eligibility, "按应用内要求完成身份和银行卡等必要信息", 80)
    trust_copy = short_visual_fact(trust, "保持费用、期限、还款信息透明", 100)
    cta = "Ajukan Sekarang" if str(language or "").lower().startswith("id") else "立即申请"
    blueprints = [
        {
            "name": "费用透明计算器型",
            "goal": "把历史素材里的滑杆、金额卡和还款信息优点综合为一个新的透明费用说明创意。",
            "layout": "上方品牌区；中间放一个全新的白色计算器面板；面板内用一个大号主标题、一个额度/期限选择区、两张结果卡展示“额度/期限”和“费用说明”；底部放蓝色 CTA 与免责声明。",
            "details": "主标题建议使用印尼语表达费用透明，例如 `Pinjaman online, biaya lebih jelas`；核心卡片只使用可验证信息：%s / %s / %s；CTA 写 `%s`。" % (amount_copy, term_copy, rate_copy, cta),
            "visual": "保留蓝白金融视觉、圆角卡片和清晰层级，但重画面板、滑杆和图标；不要使用老素材原金额、原按钮排列、原 Logo 或认证标识。",
        },
        {
            "name": "三步申请流程型",
            "goal": "把历史素材的可操作界面感转化为新的申请流程创意，让用户理解从填写资料到查看结果的路径。",
            "layout": "左侧或上半区放大标题；中间使用 3 个步骤卡片：填写资料、查看额度/期限、确认费用后申请；右侧/下方放一个轻量手机界面占位，但不要复刻老素材界面。",
            "details": "主文案围绕 `Isi data, cek limit dan tenor`；每个步骤配一个简洁线性图标；产品事实引用：%s；CTA 写 `%s`。" % (eligibility_copy, cta),
            "visual": "学习老素材的信息卡片和 CTA 强度，但改为流程图式新构图；禁止出现具体未验证额度、固定到账承诺或老素材同款滑杆/表格。",
        },
        {
            "name": "可信说明海报型",
            "goal": "吸收历史素材中顶部品牌/合规信息和底部免责声明的可信表达，做一张强调透明、可控、谨慎借款的新图。",
            "layout": "顶部品牌与产品图标；中部大标题突出透明借款；下方三张信息卡分别写费用透明、期限可查看、按评估结果申请；底部保留免责声明安全区。",
            "details": "主文案建议 `Pahami biaya sebelum mengajukan`；信息卡绑定事实或保守表达：%s / %s / %s；CTA 写 `%s`。" % (trust_copy, term_copy, rate_copy, cta),
            "visual": "用蓝白主色和少量高亮色建立金融可信感；可以借鉴信息矩阵的清晰阅读顺序，但不要做成老素材同款表格，也不要复制任何水印/徽章。",
        },
    ]
    return blueprints[(index - 1) % len(blueprints)]


def build_local_iteration_demand(task, size_plan, internal_refs, reference_analysis=None, revision_instruction="", store_profile=None):
    product = str(task.get("product_name") or task.get("app_id") or "未命名产品").strip()
    country = str(task.get("country") or "").strip()
    language = str(task.get("language") or "").strip()
    description = str(task.get("description") or "").strip()
    slots = output_slots(size_plan)
    quantity = len(slots) or max(1, int(task.get("quantity") or 1))
    size_summary = ", ".join("%s x %s" % (item.get("size"), item.get("count")) for item in size_plan) or str(task.get("size") or "")
    store_profile = normalize_store_profile(store_profile, task) if store_profile else empty_store_profile(task)
    analysis_items = reference_analysis or []
    analysis_refs = [
        {"code": str(ref.get("asset_id") or "INT_REF_%02d" % index)}
        for index, ref in enumerate(internal_refs or [], 1)
    ]
    analysis_by_code = build_analysis_map(analysis_refs, analysis_items, "INT_REF")

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
    basic_lines = product_basic_info_lines(task, store_profile)
    if basic_lines:
        lines.extend(["", "## 产品基础信息", ""])
        lines.extend(basic_lines)
    lines.extend(product_copy_constraint_lines(task, store_profile))

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
                compact_analysis_value(analysis.get("transferable_points"), 0)
                or ref.get("learning_point")
                or "该素材尚未返回逐图视觉拆解；必须先补充真实图像分析后再提取可学习点，不能沿用通用模板。",
                220,
            )
            avoid = compact_text(
                compact_analysis_value(analysis.get("avoid_copying"), 0)
                or "该素材尚未返回逐图视觉拆解时，不得直接复刻；至少禁止复制具体金额、人物、背景、Logo、水印和排版细节。",
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
                "- 视觉元素：%s" % (compact_analysis_value(analysis.get("visual_elements"), 0) or "未返回"),
                "- 可迁移点：%s" % (compact_analysis_value(analysis.get("transferable_points"), 0) or "未返回"),
                "- 禁止照搬：%s" % (compact_analysis_value(analysis.get("avoid_copying"), 0) or "未返回"),
                "- 制作提醒：%s" % (compact_analysis_value(analysis.get("production_notes"), 0) or "未返回"),
                "",
            ])

    ref_codes = ", ".join(str(ref.get("asset_id") or "INT_REF_%02d" % (idx + 1)) for idx, ref in enumerate(internal_refs)) or "历史优质素材"
    lines.extend(build_iteration_synthesis_lines(internal_refs, analysis_by_code))
    lines.extend([
        "",
        "## 新创意制作要求",
        "",
        "- 历史优质素材只用于提炼可用结构和视觉优点，不作为一对一复刻模板；逐张新素材必须是新的创意组合。",
        "- 每张新素材需要明确：核心场景、版式结构、主文案方向、可用产品事实、CTA 位置和免责声明位置。",
        "- 语言、货币、产品名、额度、周期、费率和合规表述必须替换为当前产品与当前市场可验证信息；没有可验证信息时使用保守表达。",
        "- Logo 必须使用当前产品 Logo 或预留后置叠加位置；不得重新绘制近似 Logo。",
        "",
        "## 逐张素材需求",
        "",
    ])

    for slot in slots or [{"index": i, "size": str(task.get("size") or "1:1"), "dimensions": normalize_size(task.get("size"))} for i in range(1, quantity + 1)]:
        index = int(slot["index"])
        size = slot["size"]
        dimensions = slot["dimensions"]
        blueprint = iteration_creative_blueprint(index, store_profile, language)
        lines.extend([
            "### 素材 %02d" % index,
            "",
            "- 输出规格：%s（%s）" % (size, dimensions),
            "- 综合参考来源：%s（仅作为优点拆解来源，不指定单张老素材为模板）" % ref_codes,
            "- 新创意方向：%s" % blueprint["name"],
            "- 需求目标：%s" % (description or blueprint["goal"]),
            "- 构图方向：%s" % blueprint["layout"],
            "- 画面细节：%s" % blueprint["details"],
            "- 视觉要求：%s" % blueprint["visual"],
            "- 文案要求：使用 %s 语言；主标题、信息卡和 CTA 必须完整可读；不要照搬旧素材中的具体金额、周期、费率、认证标识或按钮文案。" % (language or "目标市场"),
            "- 验收标准：尺寸正确；这是综合拆解后的新创意，不是一对一老素材迭代；没有乱码文字、没有未验证金融承诺、没有旧素材品牌/水印/徽章残留。",
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
    store_profile = fetch_store_profile(task)
    demand_text = build_local_iteration_demand(task, size_plan, internal_refs, reference_analysis, revision_instruction, store_profile)
    markdown_path, markdown_url, evidence_path = local_task_artifacts(task_id, "iteration")
    markdown_path.write_text(demand_text, encoding="utf-8")
    evidence = {
        "provider": "local_iteration",
        "task_kind": "iteration",
        "internal_evidence": internal_evidence,
        "internal_refs": internal_refs,
        "reference_analysis": reference_analysis,
        "store_profile": store_profile,
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
