#!/usr/bin/env python3
"""Explicit deployment acceptance; never scheduled. Does not print credentials."""
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

from common import Config, CosUploader, atomic_json


def main():
    config = Config()
    validation = config.root / "validation"
    validation.mkdir(exist_ok=True)
    token = config.tokens()[0]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    base = "http://127.0.0.1:%d/api/image-generation/tasks" % config.port
    record = validation / "canary.json"
    if sys.argv[1] == "submit":
        if record.exists():
            print(record.read_text())
            return
        from PIL import Image, ImageDraw
        path = validation / "reference.png"
        image = Image.new("RGB", (512, 512), "#f5ead8")
        draw = ImageDraw.Draw(image)
        draw.ellipse((142, 140, 370, 368), fill="#e98a34")
        draw.rectangle((90, 368, 422, 406), fill="#70543c")
        image.save(path)
        reference = CosUploader(config).upload({"task_id": "validation-20261113", "item_index": 0, "image_index": 0}, path)
        payload = {"request_id": "deployment-canary-20261113-v1", "items": [
            {"image_urls": [reference], "prompt": "Create a polished square product photograph inspired by this orange sphere on a wooden shelf. Make the sphere a blue ceramic vase, natural studio lighting, no text."},
            {"image_urls": [reference], "prompt": "Create a square watercolor illustration inspired by this orange sphere on a wooden shelf. Make the sphere a small orange pumpkin with a green stem. No text."},
            {"image_urls": [reference + ".missing"], "prompt": "Generate one image from this reference."}
        ]}
        start = time.monotonic()
        with urlopen(Request(base, data=json.dumps(payload).encode(), headers=headers), timeout=15) as response:
            result = json.load(response)
            result["submit_http_status"] = response.status
        result["submit_seconds"] = round(time.monotonic() - start, 3)
        result["reference_url"] = reference
        atomic_json(record, result)
        print(json.dumps(result))
    else:
        task = json.loads(record.read_text())["task_id"]
        with urlopen(Request(base + "/" + task, headers=headers), timeout=15) as response:
            result = json.load(response)
        atomic_json(validation / "latest-status.json", result)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
