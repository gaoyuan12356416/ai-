"""Dependency-free helpers shared by independently deployed native sidecars."""
import hashlib
import json
import re

from .adapters import AdapterError


def dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def claim_hash(claim):
    token = str(claim.get("claim_token") or "")
    if len(token) < 16:
        raise AdapterError("dispatch_claim_invalid")
    return hashlib.sha256(token.encode()).hexdigest()


def prepare_capabilities(channel, profile, backend, job):
    required = ["prepare:" + channel, "backend:" + backend, "profile:" + profile]
    if "executor_capabilities" in job and (channel == "fb" or "random-overlay" in profile):
        catalogs = sorted({value for value in job["executor_capabilities"]
                           if isinstance(value, str) and re.fullmatch("catalog:" + channel + ":[a-f0-9]{64}", value)})
        if len(catalogs) != 1:
            raise AdapterError("canonical_render_catalog_unavailable")
        required.extend(catalogs)
    return required
