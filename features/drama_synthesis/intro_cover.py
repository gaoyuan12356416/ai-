"""Canonical sRGB/JFIF input for the existing fixed-color intro renderer.

Normalize only a private frozen copy. Legacy FFmpeg JPEGs need not contain
APP0/JFIF; that marker alone is not proof of image validity or color space.
"""
import io
import os

from PIL import Image, ImageCms, ImageOps

from .core import DramaSynthesisError


MAX_COVER_BYTES = 32 * 1024 * 1024
MAX_COVER_PIXELS = 24_000_000


def cover_error(code="drama_intro_cover_invalid"):
    return DramaSynthesisError(code, {
        "drama_intro_cover_invalid": "片头封面无法解码或尺寸超限，请检查封面图片",
        "drama_intro_cover_color_unsupported": "片头封面的色彩信息无法转换为 sRGB，请更换封面",
        "drama_intro_cover_source_changed": "片头封面在读取时发生变化，已停止制作，请重试",
    }[code], 422)


def canonicalize_frozen_cover(path):
    """Decode and rewrite one private file as RGB JFIF; never edit the source.

Unprofiled RGB/gray web images use the sRGB convention. Embedded ICC profiles
are converted, not discarded. Unprofiled CMYK/other ambiguous modes fail.
Transparency is composited on white and EXIF orientation is applied once.
"""
    try:
        if os.path.islink(path) or not 0 < os.path.getsize(path) <= MAX_COVER_BYTES:
            raise cover_error()
        with Image.open(path) as source:
            if (source.format not in {"JPEG", "PNG", "WEBP"} or
                    source.width * source.height > MAX_COVER_PIXELS or
                    getattr(source, "n_frames", 1) != 1):
                raise cover_error()
            source.load()  # fully decode: headers and extension are insufficient
            icc = source.info.get("icc_profile")
            oriented = ImageOps.exif_transpose(source)
            alpha = oriented.convert("RGBA").getchannel("A") if (
                "A" in oriented.getbands() or "transparency" in oriented.info
            ) else None
            if icc:
                try:
                    profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                    color_input = oriented
                    if oriented.mode in {"RGBA", "P", "PA"}:
                        color_input = oriented.convert("RGB")
                    elif oriented.mode == "LA":
                        color_input = oriented.convert("L")
                    rgb = ImageCms.profileToProfile(
                        color_input, profile, ImageCms.createProfile("sRGB"), outputMode="RGB",
                    )
                except Exception:
                    raise cover_error("drama_intro_cover_color_unsupported") from None
            else:
                if oriented.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
                    raise cover_error("drama_intro_cover_color_unsupported")
                rgb = oriented.convert("RGB")
            if alpha is not None:
                background = Image.new("RGB", rgb.size, "white")
                background.paste(rgb, mask=alpha)
                rgb = background
            # A fresh image strips EXIF/ICC/Adobe markers after pixel conversion.
            clean = Image.new("RGB", rgb.size)
            clean.paste(rgb)
            encoded = io.BytesIO()
            clean.save(encoded, format="JPEG", quality=95, subsampling=0)
        with open(path, "wb") as output:
            output.write(encoded.getvalue())
            output.flush()
            os.fsync(output.fileno())
    except DramaSynthesisError:
        raise
    except Exception:
        raise cover_error() from None
