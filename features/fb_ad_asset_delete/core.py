"""Pure scope and response helpers. This module performs no I/O."""
import json
import re
from datetime import datetime, timezone

PHASES = ("creative", "ad", "video")
STATUSES = ("pending", "deleted", "already_deleted", "failed", "blocked", "unknown", "in_progress")
TERMINAL_SUCCESS = ("deleted", "already_deleted")
ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


class AssetError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ids(value, limit=50, numeric=False):
    values = value if isinstance(value, list) else re.split(r"[\s,，;；]+", str(value or ""))
    result = []
    for raw in values:
        if not isinstance(raw, (str, int)) or isinstance(raw, bool):
            raise AssetError("invalid_id", "ID 必须是文本，不能包含对象或数组")
        item = str(raw).strip()
        if not item:
            continue
        if not ID_PATTERN.fullmatch(item) or (numeric and not re.fullmatch(r"[1-9][0-9]*", item)):
            raise AssetError("invalid_id", "ID 格式不正确，请按行输入完整 ID（最多 64 个字符）")
        if item not in result:
            result.append(item)
    if not result:
        raise AssetError("missing_ids", "请输入 ID 并选择投放产品")
    if len(result) > limit:
        raise AssetError("too_many_ids", "本次最多支持 %d 个 ID" % limit)
    return result


def normalize_input(payload):
    kind = payload.get("input_type", "content_id")
    if kind not in ("content_id", "series_code"):
        raise AssetError("invalid_input_type", "请选择剧 ID 或资源 ID")
    ids = parse_ids(payload.get("ids"), 50)
    if kind == "series_code":
        ids = list(dict.fromkeys(x.upper() for x in ids))
    return kind, ids, parse_ids(payload.get("product_ids"), 20, numeric=True)


def normalize_phases(phases):
    if not isinstance(phases, list) or not phases or any(x not in PHASES for x in phases):
        raise AssetError("invalid_phases", "至少选择一个有效的删除阶段")
    return [x for x in PHASES if x in phases]


def actor_key(session):
    if not session or not session.get("user_id"):
        raise AssetError("login_required", "请先登录后台", 401)
    # JSON prevents collisions between tenant/user IDs containing delimiters.
    return json.dumps([str(session.get("tenant_key") or ""), str(session["user_id"])], separators=(",", ":"))


def meta_id(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,31}", value):
        raise AssetError("invalid_meta_id", "Meta 对象 ID 无效")
    return value


def account_id(value):
    return meta_id(str(value or "").removeprefix("act_"))


def stored_ids(value):
    """Decode known scalar, JSON-list and CSV source fields without guessing IDs."""
    if isinstance(value, (int, list)):
        raw = value
    else:
        text = str(value or "").strip()
        if text in ("", "0", "NULL", "null"):
            return []
        try:
            raw = json.loads(text) if text.startswith("[") else text
        except ValueError:
            return []
    if isinstance(raw, list):
        values = raw
    else:
        values = re.split(r"[,;\s]+", str(raw))
    return sorted({str(x).strip() for x in values if re.fullmatch(r"[1-9][0-9]{0,31}", str(x).strip())})


def stored_ids_complete(value):
    text = str(value or "").strip()
    if text in ("", "0", "NULL", "null"):
        return True
    try:
        values = value if isinstance(value, list) else json.loads(text) if text.startswith("[") else re.split(r"[,;\s]+", text)
    except ValueError:
        return False
    return isinstance(values, list) and all(not isinstance(x, (bool, dict, list)) and re.fullmatch(r"[1-9][0-9]{0,31}", str(x).strip()) for x in values)


def stored_video_ids(value):
    """Extract complete IDs from the historical VARCHAR(512) video field.

    A capacity-length CSV may end halfway through a numeric ID. Retain the
    complete prefix, never turn that partial tail into a different Video ID.
    """
    if isinstance(value, str) and len(value) >= 512 and not value.lstrip().startswith("["):
        value = re.sub(r"[^,;\s]+$", "", value)
    return stored_ids(value)


def content_markers(*names):
    ids = set()
    for name in names:
        text = str(name or "")
        ids.update(re.findall(r"(?i)(?<![A-Za-z0-9])contentid\[([A-Za-z0-9_.:-]{1,64})\]", text))
        ids.update(re.findall(r"\$([A-Za-z0-9_.:-]{1,64})@", text))
    return ids


def creative_video_ids(creative):
    ids = set()
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "video_id" and re.fullmatch(r"[1-9][0-9]{0,31}", str(item or "")):
                    ids.add(str(item))
                elif isinstance(item, (list, dict)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(creative)
    return sorted(ids)


def summary(objects):
    total = dict.fromkeys(STATUSES, 0)
    total.update(dict.fromkeys(PHASES, 0))
    total["total"] = len(objects)
    stages = {kind: dict(total=0, **dict.fromkeys(STATUSES, 0)) for kind in PHASES}
    for obj in objects:
        kind, status = obj["kind"], obj.get("status", "pending")
        total[kind] += 1
        stages[kind]["total"] += 1
        if status in STATUSES:
            total[status] += 1
            stages[kind][status] += 1
    return total, stages


def redact(value):
    """Only expose error classifications, never request URLs or credentials."""
    text = str(value or "")
    text = re.sub(r"(?i)(access[_-]?token|authorization|password|appsecret_proof)[\s=:]+[^\s&,;]+", r"\1=[redacted]", text)
    text = re.sub(r"\bEAA[A-Za-z0-9_-]{20,}\b", "[redacted]", text)
    text = re.sub(r"https?://\S+", "[request URL omitted]", text)
    return text[:600]
