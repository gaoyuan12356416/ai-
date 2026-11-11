"""Small runtime helpers; shared production taxonomy remains owned by its service."""
from __future__ import annotations

import json
import os
from pathlib import Path


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
