"""Select a local TT renderer from the task's frozen profile, never by fallback."""
import hashlib
from dataclasses import replace
from pathlib import Path


# H264 profiles already distinguish the non-random media contract. CPU uses
# libx264 explicitly; GPU uses the corresponding NVENC encoder.
PROFILE_SPECS = {
    "tt-post-hevc-720x1280-v2": ("branded_preview", "hevc"),
    "tt-post-h264-720x1280-v2": ("branded_preview", "h264"),
    "tt-post-direct-clean-hevc-720x1280-v1": ("direct_clean", "hevc"),
    "tt-post-direct-clean-h264-720x1280-v1": ("direct_clean", "h264"),
    "tt-post-direct-outro-hevc-720x1280-v2": ("direct_outro", "hevc"),
    "tt-post-direct-outro-h264-720x1280-v2": ("direct_outro", "h264"),
    "tt-post-source-direct-v1": ("source_direct", "source"),
    "tt-post-random-overlay-hevc-720x1280-v3": ("random_overlay", "hevc"),
    "tt-post-random-overlay-h264-720x1280-v3": ("random_overlay", "h264"),
    "tt-post-random-overlay-libx264-720x1280-v1": ("random_overlay", "cpu"),
}


class TTProfileError(ValueError):
    pass


def supported_profiles(backend):
    if backend == "cpu":
        return {name for name, (mode, codec) in PROFILE_SPECS.items()
                if codec in {"source", "cpu"} or codec == "h264" and mode != "random_overlay"}
    if backend == "gpu":
        return {name for name, (_, codec) in PROFILE_SPECS.items() if codec != "cpu"}
    return set()


def file_sha256(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise TTProfileError("tt_profile_asset_unavailable")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_capabilities(config):
    if config.media_mode != "direct_outro":
        return set()
    outro = file_sha256(config.fixed_outro_path)
    if outro != config.fixed_outro_sha256:
        raise TTProfileError("tt_outro_fingerprint_mismatch")
    return {"asset:tt-outro:" + outro, "asset:tt-font:" + file_sha256(config.font_file)}


def select_config(base, profile, backend):
    """Preserve the default renderer and build independent routed processors."""
    if profile not in supported_profiles(backend):
        raise TTProfileError("tt_renderer_profile_unsupported")
    mode, codec = PROFILE_SPECS[profile]
    encoder = "libx264" if backend == "cpu" else "hevc_nvenc" if codec in {"hevc", "source"} else "h264_nvenc"
    config = replace(base, profile=profile, media_mode=mode, video_encoder=encoder,
                     compositor_backend=base.compositor_backend if mode == "random_overlay" else "cpu_legacy",
                     work_root=base.work_root if profile == base.profile else base.work_root / "profiles" / profile)
    if mode == "random_overlay" and not config.random_overlay_assets:
        raise TTProfileError("tt_random_assets_unavailable")
    if mode == "direct_outro":
        asset_capabilities(config)
    return config
