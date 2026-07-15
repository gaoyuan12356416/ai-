#!/usr/bin/env python3
import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
import re
import sys
import uuid


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_SOURCE = os.path.join(ROOT, "doc", "deployment", "playable-preview-api.md")
DEFAULT_HTML_SOURCE = os.path.join(ROOT, "doc", "deployment", "playable-preview-api.html")
DEFAULT_ENV_FILE = os.path.join(ROOT, ".env")
DOC_OBJECT_KEY = "ad-materials/docs/playable-preview-api.md"
HTML_DOC_OBJECT_KEY = "ad-materials/docs/playable-preview-api.html"
CANONICAL_ENDPOINT = "https://ai.yingliangads.com/api/fb-playable/preview"
LEGACY_ENDPOINT = "/api/ad-material/playable-preview"
MARKDOWN_CONTENT_TYPE = "text/markdown; charset=utf-8"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"
CACHE_CONTROL = "no-cache"


class HTMLDocumentAudit(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.counts = {}
        self.forbidden_tags = []
        self.event_attributes = []
        self.source_hash = ""
        self.csp = ""

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag in {"script", "iframe", "object", "embed", "form", "base"}:
            self.forbidden_tags.append(tag)
        attr_map = {str(key or "").lower(): str(value or "") for key, value in attrs}
        for key in attr_map:
            if key.startswith("on"):
                self.event_attributes.append("%s.%s" % (tag, key))
        if tag == "meta" and attr_map.get("name", "").lower() == "playable-doc-source-sha256":
            self.source_hash = attr_map.get("content", "").lower()
        if tag == "meta" and attr_map.get("http-equiv", "").lower() == "content-security-policy":
            self.csp = attr_map.get("content", "")


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
    text = payload.decode("utf-8")
    if not payload.startswith(b"# "):
        raise ValueError("playable preview API document must start with a Markdown title")
    normalized_payload = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return payload, {
        "source": os.path.abspath(path),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "normalized_sha256": hashlib.sha256(normalized_payload).hexdigest(),
    }


def html_document_details(path, markdown_sha256):
    with open(path, "rb") as handle:
        payload = handle.read()
    text = payload.decode("utf-8")
    if not text.lstrip().lower().startswith("<!doctype html>"):
        raise ValueError("playable preview HTML document must start with a doctype")
    audit = HTMLDocumentAudit()
    audit.feed(text)
    audit.close()
    if audit.source_hash != markdown_sha256.lower():
        raise ValueError("playable preview HTML document is stale for the Markdown source")
    if audit.forbidden_tags:
        raise ValueError("playable preview HTML document contains active embedded tags")
    if audit.event_attributes:
        raise ValueError("playable preview HTML document contains event-handler attributes")
    if "default-src 'none'" not in audit.csp or "style-src 'unsafe-inline'" not in audit.csp:
        raise ValueError("playable preview HTML document must carry the controlled CSP")
    required_counts = {"h1": 1, "h2": 1, "pre": 1, "table": 1}
    if any(audit.counts.get(tag, 0) < minimum for tag, minimum in required_counts.items()):
        raise ValueError("playable preview HTML document is missing required rendered structures")
    if "javascript:" in text.lower():
        raise ValueError("playable preview HTML document contains a javascript URL")
    if LEGACY_ENDPOINT in text:
        raise ValueError("legacy playable preview endpoint must not appear in the HTML document")
    api_urls = sorted(set(re.findall(r"https://ai\.yingliangads\.com/api/[^\s'\"<]+", text)))
    if api_urls != [CANONICAL_ENDPOINT]:
        raise ValueError("HTML document must contain only the canonical playable preview API URL")
    return payload, {
        "html_source": os.path.abspath(path),
        "html_size": len(payload),
        "html_sha256": hashlib.sha256(payload).hexdigest(),
    }


def write_atomic(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(payload)
    os.replace(temporary, path)


def stage_local_bundle(items):
    suffix = ".stage-" + uuid.uuid4().hex
    staged = []
    snapshots = {}
    try:
        for item in items:
            target = item["target"]
            os.makedirs(os.path.dirname(target), exist_ok=True)
            snapshots[target] = None
            if os.path.isfile(target):
                with open(target, "rb") as handle:
                    snapshots[target] = handle.read()
            temporary = target + suffix
            with open(temporary, "wb") as handle:
                handle.write(item["payload"])
                handle.flush()
                os.fsync(handle.fileno())
            staged.append({"target": target, "temporary": temporary})
        return staged, snapshots
    except Exception:
        for item in staged:
            try:
                os.remove(item["temporary"])
            except OSError:
                pass
        raise


def cleanup_local_stages(staged):
    for item in staged:
        try:
            os.remove(item["temporary"])
        except OSError:
            pass


def commit_local_bundle(staged, snapshots):
    replaced = []
    try:
        for item in staged:
            os.replace(item["temporary"], item["target"])
            replaced.append(item["target"])
    except Exception:
        for target in reversed(replaced):
            previous = snapshots.get(target)
            try:
                if previous is None:
                    os.remove(target)
                else:
                    write_atomic(target, previous)
            except OSError:
                pass
        cleanup_local_stages(staged)
        raise


def cos_exception_status(exc):
    getter = getattr(exc, "get_status_code", None)
    if callable(getter):
        try:
            return int(getter())
        except (TypeError, ValueError):
            pass
    return None


def cos_exception_code(exc):
    getter = getattr(exc, "get_error_code", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            pass
    return ""


def read_cos_object(client, bucket, key):
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if cos_exception_status(exc) == 404 or cos_exception_code(exc) in {"NoSuchKey", "NoSuchResource"}:
            return None
        raise
    body = response.get("Body")
    stream = body.get_raw_stream() if hasattr(body, "get_raw_stream") else body
    if stream is None or not hasattr(stream, "read"):
        raise RuntimeError("COS get_object response is missing a readable body")
    try:
        payload = stream.read()
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    if not isinstance(payload, bytes):
        payload = bytes(payload)
    headers = {str(name).lower(): str(value) for name, value in response.items() if name != "Body"}
    return {
        "payload": payload,
        "etag": headers.get("etag", "").strip('"'),
        "content_type": headers.get("content-type", ""),
        "cache_control": headers.get("cache-control", ""),
    }


def put_cos_object(client, bucket, item, key=None, acl="public-read"):
    return client.put_object(
        Bucket=bucket,
        Body=item["payload"],
        Key=key or item["key"],
        EnableMD5=True,
        ACL=acl,
        ContentType=item["content_type"],
        CacheControl=CACHE_CONTROL,
    )


def verify_cos_object(client, bucket, item, key=None):
    object_key = key or item["key"]
    current = read_cos_object(client, bucket, object_key)
    if current is None:
        raise RuntimeError("published COS object is missing: %s" % object_key)
    if current["payload"] != item["payload"]:
        raise RuntimeError("published COS object body mismatch: %s" % object_key)
    if current["content_type"].lower() != item["content_type"].lower():
        raise RuntimeError("published COS object Content-Type mismatch: %s" % object_key)
    if current["cache_control"].lower() != CACHE_CONTROL:
        raise RuntimeError("published COS object Cache-Control mismatch: %s" % object_key)
    current["sha256"] = hashlib.sha256(current["payload"]).hexdigest()
    return current


def restore_cos_objects(client, bucket, items, snapshots):
    errors = []
    for item in reversed(items):
        key = item["key"]
        previous = snapshots.get(key)
        try:
            if previous is None:
                client.delete_object(Bucket=bucket, Key=key)
                if read_cos_object(client, bucket, key) is not None:
                    raise RuntimeError("rollback delete did not remove %s" % key)
            else:
                rollback_item = dict(item)
                rollback_item["payload"] = previous["payload"]
                put_cos_object(client, bucket, rollback_item)
                verify_cos_object(client, bucket, rollback_item)
        except Exception as exc:
            errors.append("%s: %s" % (key, exc))
    return errors


def publish_cos_bundle(client, bucket, items):
    snapshots = {item["key"]: read_cos_object(client, bucket, item["key"]) for item in items}
    stage_prefix = "ad-materials/docs/.playable-preview-stage/%s/" % uuid.uuid4().hex
    staged_keys = []
    fixed_started = False
    verification = {}
    try:
        for item in items:
            stage_key = stage_prefix + item["key"].rsplit("/", 1)[-1]
            staged_keys.append(stage_key)
            put_cos_object(client, bucket, item, key=stage_key, acl="private")
            verify_cos_object(client, bucket, item, key=stage_key)

        fixed_started = True
        for item in items:
            put_cos_object(client, bucket, item)
        for item in items:
            verification[item["key"]] = verify_cos_object(client, bucket, item)
        return {"snapshots": snapshots, "verification": verification}
    except Exception as exc:
        rollback_errors = restore_cos_objects(client, bucket, items, snapshots) if fixed_started else []
        if rollback_errors:
            raise RuntimeError(
                "COS document publication failed and rollback was incomplete: %s"
                % "; ".join(rollback_errors)
            ) from exc
        raise
    finally:
        for key in staged_keys:
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass


def publish_document(source, html_source, public_root):
    payload, result = document_details(source)
    html_payload, html_result = html_document_details(html_source, result["normalized_sha256"])
    result.update(html_result)

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
    bucket = os.environ["COS_BUCKET"].strip()
    target = os.path.join(public_root, "docs", "playable-preview-api.md")
    html_target = os.path.join(public_root, "docs", "playable-preview-api.html")
    items = [
        {
            "key": DOC_OBJECT_KEY,
            "target": target,
            "payload": payload,
            "content_type": MARKDOWN_CONTENT_TYPE,
        },
        {
            "key": HTML_DOC_OBJECT_KEY,
            "target": html_target,
            "payload": html_payload,
            "content_type": HTML_CONTENT_TYPE,
        },
    ]

    staged, local_snapshots = stage_local_bundle(items)
    cos_result = None
    try:
        cos_result = publish_cos_bundle(client, bucket, items)
        commit_local_bundle(staged, local_snapshots)
    except Exception as exc:
        cleanup_local_stages(staged)
        if cos_result is not None:
            rollback_errors = restore_cos_objects(client, bucket, items, cos_result["snapshots"])
            if rollback_errors:
                raise RuntimeError(
                    "local document publication failed and COS rollback was incomplete: %s"
                    % "; ".join(rollback_errors)
                ) from exc
        raise

    domain = os.environ["COS_DOMAIN"].strip().strip("/")
    md_verification = cos_result["verification"][DOC_OBJECT_KEY]
    html_verification = cos_result["verification"][HTML_DOC_OBJECT_KEY]
    result.update({
        "target": target,
        "html_target": html_target,
        "object_key": DOC_OBJECT_KEY,
        "html_object_key": HTML_DOC_OBJECT_KEY,
        "url": "https://%s/%s" % (domain, DOC_OBJECT_KEY),
        "html_url": "https://%s/%s" % (domain, HTML_DOC_OBJECT_KEY),
        "etag": md_verification["etag"],
        "html_etag": html_verification["etag"],
        "readback_verified": True,
        "published": True,
    })
    return result


def main():
    parser = argparse.ArgumentParser(description="Publish the FB playable preview API document to COS")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--html-source", default=DEFAULT_HTML_SOURCE)
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--public-root", default="")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    source = os.path.abspath(args.source)
    if not os.path.isfile(source):
        raise ValueError("API document is missing: %s" % source)
    html_source = os.path.abspath(args.html_source)
    if not os.path.isfile(html_source):
        raise ValueError("HTML API document is missing: %s" % html_source)
    _, details = document_details(source)
    _, html_details = html_document_details(html_source, details["normalized_sha256"])
    details.update(html_details)
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
    result = publish_document(source, html_source, os.path.abspath(public_root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
