#!/usr/bin/env python3
import json
import os
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen


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
    return "\n".join(compact).strip()


def clean_generation_reason(value):
    text = str(value or "").strip()
    return "" if is_noise_line(text) else text


def compact_text(value, limit=220):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def extract_product_fact_lines(demand_text, limit=10):
    wanted_terms = (
        "额度", "金额", "期限", "周期", "利息", "费率", "费用", "还款", "申请条件", "资质", "合规",
        "OJK", "AFPI", "KTP", "WNI", "IDR", "Rp", "MXN", "USD", "%", "tenor", "limit", "pinjaman",
        "bunga", "biaya",
    )
    lines = []
    seen = set()
    for raw in str(demand_text or "").splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        if "<img" in line.lower() or "http://" in line.lower() or "https://" in line.lower():
            continue
        if any(term.lower() in line.lower() for term in wanted_terms):
            clean = compact_text(line.lstrip("- ").strip(), 260)
            key = clean.lower()
            if clean and key not in seen:
                lines.append(clean)
                seen.add(key)
        if len(lines) >= limit:
            break
    return lines


def build_generation_copy_guard(task, demand_text):
    product = str(task.get("product_name") or task.get("app_id") or "当前产品").strip()
    language = str(task.get("language") or "目标语言").strip()
    country = str(task.get("country") or "目标市场").strip()
    fact_lines = extract_product_fact_lines(demand_text)
    lines = [
        "",
        "## 生图文案硬约束",
        "",
        "- 图片可见文案必须与 %s 产品直接相关，面向 %s / %s；禁止生成含义不明、和产品参数无关的泛口号。" % (product, country, language),
        "- 主标题、副文案、CTA、小字必须使用 %s 语言；不得混用其他语言、乱码或参考图里的竞品原文。" % (language or "目标语言"),
        "- 每张图必须出现当前产品名或当前产品 Logo，并至少承接 1 个需求中的产品事实、申请流程或费用透明场景。" ,
        "- 不得编造未在需求中出现的额度、期限、费率、到账速度、通过率、免审、免息、固定月供或合规背书。",
    ]
    if fact_lines:
        lines.append("- 可用产品事实/参数如下，图片文字优先从这里取：")
        for item in fact_lines[:8]:
            lines.append("  - %s" % item)
    else:
        lines.append("- 当前需求未提供可验证产品参数时，只能用“在应用内查看额度、期限和费用”等保守表达，不得写具体数字。")
    if language.lower().startswith("id"):
        lines.append("- 印尼语广告文案优先使用 `Pinjaman online`、`Limit`、`Tenor`、`Biaya/Bunga transparan`、`Ajukan Sekarang` 这类与贷款产品直接相关的表达；避免只写 `Pembayaran lebih jelas` 等弱相关文案。")
    return "\n".join(lines)


def read_payload():
    payload_path = os.environ.get("AD_MATERIAL_TASK_PAYLOAD")
    if not payload_path:
        raise SystemExit("AD_MATERIAL_TASK_PAYLOAD is required")
    return json.loads(Path(payload_path).read_text(encoding="utf-8"))


def write_output(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def download(url, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=int(os.environ.get("AD_MATERIAL_GENERATION_DOWNLOAD_TIMEOUT", "600"))) as resp:
        data = resp.read()
    if len(data) < 1024:
        raise RuntimeError("generated asset download too small: %s" % url)
    path.write_bytes(data)


def post_json(url, payload, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body or "{}")


def main():
    payload = read_payload()
    task = payload.get("task") or {}
    extra = payload.get("extra") or {}
    output_path = payload.get("output_path") or os.environ.get("AD_MATERIAL_TASK_OUTPUT")
    if not output_path:
        raise SystemExit("output_path is required")

    service_url = os.environ.get(
        "AD_MATERIAL_GENERATION_SERVICE_URL",
        "http://127.0.0.1:18797/api/ad-material-generation/generate",
    ).rstrip("/")
    timeout = int(os.environ.get("AD_MATERIAL_GENERATION_SERVICE_TIMEOUT", os.environ.get("AD_MATERIAL_COMMAND_TIMEOUT", "2400")))
    task_id = str(task.get("task_id") or os.environ.get("AD_MATERIAL_TASK_ID") or "").strip()
    if not task_id:
        raise RuntimeError("task_id is required")

    demand_text = clean_material_demand_text(task.get("demand_text") or "")
    guarded_demand_text = clean_material_demand_text(demand_text.rstrip() + "\n" + build_generation_copy_guard(task, demand_text))
    safe_task = dict(task)
    safe_task["demand_text"] = guarded_demand_text
    request_payload = {
        "task": safe_task,
        "indexes": extra.get("indexes") or [],
        "reason": clean_generation_reason(extra.get("reason") or task.get("review_reason") or ""),
        "demand_text": guarded_demand_text,
        "copy_guard": build_generation_copy_guard(task, demand_text),
    }
    result = post_json(service_url, request_payload, timeout)
    if not result.get("ok", True):
        raise RuntimeError(result.get("error") or "ad material generation service failed")

    public_root = Path(os.environ.get("AD_MATERIAL_PUBLIC_ROOT", "/usr/share/nginx/html/ad-materials"))
    local_dir = public_root / task_id
    outputs = []
    for offset, item in enumerate(result.get("outputs") or result.get("assets") or [], 1):
        index = int(item.get("asset_index") or item.get("index") or offset)
        asset_id = str(item.get("asset_id") or "%s_%02d" % (task_id, index))
        remote_url = str(item.get("public_url") or item.get("url") or "").strip()
        if not remote_url:
            continue
        suffix = int(time.time())
        local_path = local_dir / ("%s_%s.png" % (asset_id, suffix))
        download(remote_url, local_path)
        outputs.append(
            {
                "asset_id": asset_id,
                "asset_index": index,
                "name": item.get("name") or "%s_%02d" % (task.get("product_name") or "ad_material", index),
                "local_path": str(local_path),
                "generator": item.get("generator") or "codex-imagegen",
                "summary": item.get("summary") or "",
            }
        )

    if not outputs:
        raise RuntimeError("ad material generation service returned no downloadable outputs")
    write_output(output_path, {"outputs": outputs})
    print(json.dumps({"outputs": outputs}, ensure_ascii=False))


if __name__ == "__main__":
    main()
