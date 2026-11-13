"""Identities accepted by an explicit personal X publishing selection."""

import re

_MATERIAL_ID = re.compile(r"[1-9][0-9]{0,30}\Z")
_EPISODE_ID = re.compile(r"[0-9a-fA-F]{32}\Z")


def is_episode_id(value):
    return isinstance(value, str) and _EPISODE_ID.fullmatch(value) is not None


def normalize_material_id(value):
    """Keep numeric IDs intact; use one identity for either case of a resource ID."""
    if not isinstance(value, str):
        raise ValueError("素材 ID 必须是数字素材 ID 或 32 位免费集 ID 文本")
    if _MATERIAL_ID.fullmatch(value):
        return value
    if is_episode_id(value):
        return value.lower()
    raise ValueError("素材 ID 必须是数字素材 ID 或 32 位免费集 ID 文本")
