#!/usr/bin/env python3
import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen


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

    request_payload = {
        "task": task,
        "indexes": extra.get("indexes") or [],
        "reason": extra.get("reason") or task.get("review_reason") or "",
        "demand_text": task.get("demand_text") or "",
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
