"""Small runtime helpers; shared production taxonomy remains owned by its service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re


def environment(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    values.update(os.environ)
    return values


def shared(env):
    import features
    root = Path(env.get("FB_PAGE_ASSIGNMENT_SHARED_ROOT", "/root/drama_material_service"))
    if not (root / "features/x_tag_revenue/taxonomy.py").is_file():
        raise RuntimeError("shared_taxonomy_missing")
    path = str(root / "features")
    if path in features.__path__:
        features.__path__.remove(path)
    features.__path__.insert(0, path)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def log(event, **fields):
    print(encoded({"event": event, **fields}), flush=True)


def database_error(error):
    """Return only fixed diagnostic labels, never driver messages or SQL."""
    message = str(error).lower()
    code = error.args[0] if isinstance(error, Exception) and error.args else None
    if not isinstance(code, int):
        match = re.search(r"error (\d+)", message)
        code = int(match[1]) if match else None
    phase = "unknown"
    for phrase, value in (("initial communication packet", "greeting"),
                          ("authorization packet", "authentication"),
                          ("during query", "query"), ("ssl connection", "tls")):
        if phrase in message:
            phase = value
            break
    reason = "unspecified"
    for phrase, value in (("timed out", "timeout"), ("timeout", "timeout"),
                          ("connection reset", "reset"), ("connection refused", "refused"),
                          ("broken pipe", "broken_pipe"), ("lost connection", "connection_lost")):
        if phrase in message:
            reason = value
            break
    return dict(mysql_error_code=code, mysql_phase=phase, transport_reason=reason)
