"""Immutable per-item preflight outcomes for partial operator batches."""
import re


def validate_partition(account_ids, material_ids, candidates, failures):
    """Require exact, ordered original pairs; omitted targets need evidence."""
    from features.x_posts.service import XPostError, redact_text
    if not isinstance(failures, list):
        raise XPostError("invalid_request", "preflight_failures must be an array", 400)
    original = dict(zip(account_ids, material_ids))
    seen, last_index = set(), -1
    for item in candidates:
        if not isinstance(item, dict):
            raise XPostError("invalid_request", "candidate must be an object", 400)
        account = item.get("account_id")
        if isinstance(account, bool) or not isinstance(account, int) or account not in original or account in seen:
            raise XPostError("x_post_manual_account_mismatch", "候选账号与原请求不一致", 409)
        index = account_ids.index(account)
        if index <= last_index or str(item.get("material_id")) != original[account]:
            raise XPostError("x_post_manual_material_mismatch", "候选配对与原请求不一致", 409)
        seen.add(account)
        last_index = index
    normalized = []
    for item in failures:
        if not isinstance(item, dict):
            raise XPostError("invalid_request", "failure must be an object", 400)
        account = item.get("account_id")
        code = str(item.get("error_code") or "")
        if (isinstance(account, bool) or not isinstance(account, int) or account not in original or account in seen
                or str(item.get("material_id")) != original[account]
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code)):
            raise XPostError("x_post_manual_scope_mismatch", "预检失败项与原请求不一致", 409)
        seen.add(account)
        normalized.append({"account_id": account, "material_id": original[account],
                           "error_code": code, "error_message": redact_text(item.get("error_message"), 240)})
    if seen != set(account_ids):
        raise XPostError("x_post_manual_candidate_shortage", "每个冻结目标都必须有候选或失败记录", 409)
    return sorted(normalized, key=lambda item: account_ids.index(item["account_id"]))


class PreparedManual(list):
    def __init__(self, values=(), failures=()):
        super().__init__(values)
        self.preflight_failures = list(failures)
