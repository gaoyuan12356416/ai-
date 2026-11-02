"""Execute the existing reference-image generation workflow on a worker node."""
import base64
import hashlib
import io
import re
from pathlib import Path

from .executors import ExecutionError


def inline_image(data, mime="image/jpeg"):
    if not isinstance(data, bytes) or not 0 < len(data) <= 2 * 1024 * 1024:
        raise ExecutionError("youtube_image_size_invalid")
    return {"data_base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mime": mime}


def image_bytes(value):
    try:
        if not isinstance(value, dict) or not 0 < int(value["size"]) <= 2 * 1024 * 1024:
            raise ValueError()
        data = base64.b64decode(value["data_base64"], validate=True)
        if len(data) != value["size"] or hashlib.sha256(data).hexdigest() != value["sha256"]:
            raise ValueError()
        return data
    except (KeyError, ValueError, TypeError):
        raise ExecutionError("youtube_image_identity_invalid") from None


def generate_cover(request, work_root, context, generator=None):
    from features.youtube_auto_publish.runtime import generate_cover_factory
    from features.youtube_auto_publish.images import decode_cover
    task = dict(request["task"])
    if not re.fullmatch(r"[a-f0-9]{32}", str(task.get("id") or "")):
        raise ExecutionError("youtube_preparation_identity_invalid")
    reference = task["reference_cover"]
    asset_id = str(reference.get("asset_id") or "")
    if not re.fullmatch(r"[a-f0-9]{32}", asset_id):
        raise ExecutionError("youtube_reference_identity_invalid")
    data = image_bytes(request["reference_image"])
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise ExecutionError("youtube_reference_identity_invalid")
    root = Path(work_root) / ("youtube-" + task["id"])
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = assets / (asset_id + ".jpg")
    if target.is_symlink() or (target.exists() and target.read_bytes() != data):
        raise ExecutionError("youtube_reference_identity_changed")
    if not target.exists():
        with target.open("xb") as handle:
            handle.write(data)
    context.phase("cover_generation")
    result = (generator or generate_cover_factory(root))(task, task["versions"][-1])
    context.check()
    raw = result if isinstance(result, bytes) else Path(result).read_bytes()
    background = decode_cover(raw, generated=True)
    if max(background.size) > 3840:
        background.thumbnail((3840, 2160))
    for quality in (90, 82, 72):
        output = io.BytesIO()
        background.save(output, format="JPEG", quality=quality, optimize=True)
        encoded = output.getvalue()
        if len(encoded) <= 2 * 1024 * 1024:
            break
    return {"status": "ready", "cover": inline_image(encoded), "reference_sha256": reference["sha256"], "version": task["current_version"]}
