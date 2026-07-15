#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_SOURCE = os.path.join(ROOT, "doc", "deployment", "playable-preview-api.md")
DEFAULT_ENV_FILE = os.path.join(ROOT, ".env")
DOC_OBJECT_KEY = "ad-materials/docs/playable-preview-api.md"


def load_env_file(path):
    if not path or not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ[key] = value


def document_details(path):
    with open(path, "rb") as handle:
        payload = handle.read()
    payload.decode("utf-8")
    if not payload.startswith(b"# "):
        raise ValueError("playable preview API document must start with a Markdown title")
    return payload, {
        "source": os.path.abspath(path),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def publish_document(source, public_root):
    payload, result = document_details(source)
    target = os.path.join(public_root, "docs", "playable-preview-api.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = target + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(payload)
    os.replace(temporary, target)

    required = ("COS_SECRET_ID", "COS_SECRET_KEY", "COS_BUCKET", "COS_REGION", "COS_DOMAIN")
    missing = [key for key in required if not str(os.environ.get(key) or "").strip()]
    if missing:
        raise ValueError("missing COS environment variables: %s" % ", ".join(missing))

    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as exc:
        raise RuntimeError("qcloud_cos is required to publish the API document") from exc

    config = CosConfig(
        Region=os.environ["COS_REGION"].strip(),
        SecretId=os.environ["COS_SECRET_ID"].strip(),
        SecretKey=os.environ["COS_SECRET_KEY"].strip(),
        Timeout=60,
        KeepAlive=False,
    )
    client = CosS3Client(config)
    with open(target, "rb") as handle:
        response = client.put_object(
            Bucket=os.environ["COS_BUCKET"].strip(),
            Body=handle,
            Key=DOC_OBJECT_KEY,
            EnableMD5=True,
            ACL="public-read",
            ContentType="text/markdown; charset=utf-8",
            CacheControl="no-cache",
        )

    domain = os.environ["COS_DOMAIN"].strip().strip("/")
    result.update({
        "target": target,
        "object_key": DOC_OBJECT_KEY,
        "url": "https://%s/%s" % (domain, DOC_OBJECT_KEY),
        "etag": str(response.get("ETag") or "").strip('"'),
        "published": True,
    })
    return result


def main():
    parser = argparse.ArgumentParser(description="Publish the FB playable preview API document to COS")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--public-root", default="")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    source = os.path.abspath(args.source)
    if not os.path.isfile(source):
        raise ValueError("API document is missing: %s" % source)
    _, details = document_details(source)
    if args.check_only:
        details["published"] = False
        print(json.dumps(details, ensure_ascii=False, indent=2))
        return 0

    load_env_file(os.path.abspath(args.env_file))
    public_root = (
        str(args.public_root or "").strip()
        or str(os.environ.get("AD_MATERIAL_PUBLIC_ROOT") or "").strip()
        or "/usr/share/nginx/html/ad-materials"
    )
    result = publish_document(source, os.path.abspath(public_root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
