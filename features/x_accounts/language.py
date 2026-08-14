"""Canonical drama-language helpers shared by all X publishing paths."""

from __future__ import annotations

import re


DEFAULT_DRAMA_LANGUAGE = "en"
MAX_DRAMA_LANGUAGE_LENGTH = 32
_DRAMA_LANGUAGE_RE = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_PRIMARY_LANGUAGE_ALIASES = {
    # Historical X Auto templates used ``jp``.  Keep those frozen records
    # compatible while persisting the standards-based Japanese code ``ja``.
    "jp": "ja",
}


def canonical_drama_language(value, *, default=DEFAULT_DRAMA_LANGUAGE):
    """Return a lowercase, hyphenated language tag or raise ``ValueError``."""
    raw = default if value is None or str(value).strip() == "" else value
    language = str(raw).strip().lower().replace("_", "-")
    if len(language) < 2 or len(language) > MAX_DRAMA_LANGUAGE_LENGTH:
        raise ValueError("drama language must be 2-32 characters")
    if not _DRAMA_LANGUAGE_RE.fullmatch(language):
        raise ValueError("drama language must be a language tag such as en or pt-br")
    parts = language.split("-")
    parts[0] = _PRIMARY_LANGUAGE_ALIASES.get(parts[0], parts[0])
    return "-".join(parts)


def same_drama_language(left, right):
    """Compare language tags with legacy aliases such as ``jp`` and ``ja``."""
    try:
        return canonical_drama_language(left) == canonical_drama_language(right)
    except ValueError:
        return False
