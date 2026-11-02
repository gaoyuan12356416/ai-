"""Bounded failure text for receipts; never serialize arbitrary exceptions."""
import re
from urllib.parse import unquote


def safe_failure_message(value):
    if not isinstance(value, str):
        return ""
    try:
        return _sanitize_message(value)
    except Exception:
        return ""  # Optional diagnostics must never prevent the original receipt.


def _sanitize_message(value):
    from features.x_posts.service import redact_text
    text = redact_text(unquote(value[:8192]), 8192)
    text = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", "[redacted-url]", text)
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+(?:\[redacted\]|[^\s,;]+)", "[redacted-authorization]", text)
    # The existing X redactor may already have replaced the first word of a
    # quoted credential. Its remaining words cannot be safely reconstructed,
    # so discard the suffix starting at a credential assignment altogether.
    key = r"[\w.-]*(?:token|secret|password|passwd|api[_-]?key|authorization|cookie|credential|signature|code_verifier)[\w.-]*"
    credential = re.search(r"(?i)[\"']?" + key + r"[\"']?\s*[:=]", text)
    if credential:
        text = text[:credential.start()] + "[redacted-credentials]"
    text = re.sub(r"(?i)\b(" + key + r")\s+(?!\[redacted)([^\s,;&]+)", r"\1 [redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted]", text)
    text = re.sub(r"[A-Za-z0-9_+/=-]{32,}", "[redacted]", text)
    return " ".join(text.split())[:500]


def failure_fields(message):
    text = safe_failure_message(message)
    if not text:
        return {}
    fields = {"error_message": text}
    # XPostError.status is the sidecar HTTP status (often 502), not the
    # platform response. Only the transport's explicit HTTP marker is used.
    statuses = set(re.findall(r"\(HTTP ([1-5][0-9]{2})\)", text))
    if len(statuses) == 1:
        fields["upstream_http_status"] = int(next(iter(statuses)))
    return fields


def exception_failure_fields(exc):
    try:
        from features.x_posts.service import XPostError
        return failure_fields(str(exc)) if type(exc) is XPostError else {}
    except Exception:
        return {}
