"""Private, task-addressable request bodies; never copy them into journal/MySQL."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re


_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_COLON = re.compile(r"\s*:\s*")
_SENSITIVE = {
    "authorization", "proxyauthorization", "token", "accesstoken", "refreshtoken",
    "idtoken", "apitoken", "apikey", "clientsecret", "password", "secret", "secretkey",
    "credential", "credentials",
}
_MASK = "[REDACTED]"


def redact_body(raw, secrets=()):
    """Keep whitespace, duplicate keys and types, replacing only secret spans."""
    try:
        original, utf8_valid = raw.decode("utf-8"), True
    except UnicodeError:
        original, utf8_valid = raw.decode("utf-8", errors="replace"), False
    text = original
    for secret in secrets:
        if secret:
            text = text.replace(secret, _MASK)
    decoder = json.JSONDecoder()
    parts, end = [], 0
    for match in _STRING.finditer(text):
        if match.start() < end:
            continue
        try:
            value = json.loads(match.group())
        except ValueError:
            continue
        colon = _COLON.match(text, match.end())
        key = re.sub(r"[^a-z0-9]", "", value.lower())
        if colon and (key in _SENSITIVE or key.endswith(("token", "password", "secret"))):
            start = colon.end()
            try:
                _, stop = decoder.raw_decode(text, start)
            except (ValueError, RecursionError):
                # A malformed secret value must not expose the remaining body.
                stop = len(text)
            parts.extend((text[end:start], json.dumps(_MASK)))
            end = stop
        else:
            safe = value
            for secret in secrets:
                if secret:
                    safe = safe.replace(secret, _MASK)
            if safe != value:
                parts.extend((text[end:match.start()], json.dumps(safe)))
                end = match.end()
    parts.append(text[end:])
    body = "".join(parts)
    return body, body != original, utf8_valid


class RequestBodyLog:
    def __init__(self, root, *, secrets=()):
        self.root, self.secrets = Path(root), tuple(secrets)

    def save(self, task_id, raw, *, content_type="", client_ip=""):
        now = datetime.now(timezone.utc)
        day = now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = self.root / day
        directory.mkdir(exist_ok=True, mode=0o700)
        body, redacted, utf8_valid = redact_body(raw, self.secrets)
        record = dict(task_id=task_id, received_at=now.isoformat(), client_ip=client_ip,
                      content_type=content_type, body_bytes=len(raw), raw_body=body,
                      redacted=redacted, utf8_valid=utf8_valid)
        path = directory / (task_id + ".json")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
        return path
