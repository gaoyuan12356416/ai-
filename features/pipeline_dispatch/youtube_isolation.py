"""Serialize active channel writes; quarantine only an ended write's media."""
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit


def identity_resources(channel_id, material_id="", source_url=""):
    channel = str(channel_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", channel):
        return [], "", []
    channel_key = "youtube:channel:" + channel
    media = []
    if material_id:
        media.append("youtube:material:" + channel + ":" + hashlib.sha256(str(material_id).encode()).hexdigest())
    if source_url:
        parsed = urlsplit(str(source_url))
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username:
            # Signed query strings are not a new video identity.
            canonical = urlunsplit(("https", parsed.netloc.lower(), parsed.path, "", ""))
            media.append("youtube:source:" + channel + ":" + hashlib.sha256(canonical.encode()).hexdigest())
    return sorted([channel_key, *media]), channel_key, sorted(media)


def write_active(row):
    # An elapsed lease is NOT evidence that a platform write has ended.
    # The native failure/completion path explicitly relinquishes ownership.
    return bool(row.get("lease_owner"))
