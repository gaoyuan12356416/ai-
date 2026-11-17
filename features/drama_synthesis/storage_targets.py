"""Keep historical COS receipts readable during an explicit bucket cutover."""
import json
import os
from urllib.parse import urlsplit

from .local_checkpoint import read_record


def legacy_domains():
    value = json.loads(os.environ.get("COS_LEGACY_BUCKET_DOMAINS", "{}"))
    if not isinstance(value, dict):
        raise ValueError("COS_LEGACY_BUCKET_DOMAINS must be a mapping")
    for bucket, domain in value.items():
        if not isinstance(bucket, str) or not isinstance(domain, str):
            raise ValueError("invalid legacy COS target")
        parsed = urlsplit("https://" + domain)
        if not bucket or not domain or parsed.netloc != domain or parsed.path or parsed.username or parsed.password:
            raise ValueError("invalid legacy COS domain")
    return value


def checkpoint_target(path, bucket, domain):
    """Select only a configured old bucket; resume_upload still validates all facts."""
    record = read_record(path)
    previous = (record or {}).get("target", {})
    previous = previous.get("bucket") if isinstance(previous, dict) else None
    if previous and previous != bucket:
        old_domain = legacy_domains().get(previous)
        if not old_domain:
            raise ValueError("unconfigured checkpoint bucket")
        return previous, old_domain
    return bucket, domain


def artifact_url(bucket, key, default_bucket, url_for_key):
    if bucket == default_bucket:
        return url_for_key(key)
    domain = legacy_domains().get(bucket)
    if not domain:
        raise ValueError("unconfigured artifact bucket")
    return "https://" + domain + "/" + key.lstrip("/")
