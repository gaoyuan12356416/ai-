"""Independent TikTok automatic publishing templates.

This package intentionally does not import or instantiate the legacy
``TTPostStore``.  The only supported integration with the legacy publisher is
through explicit read-only adapters and the already hardened GPU client.
"""

from .client import TT_AUTO_ADMIN_PREFIX

__all__ = ["TT_AUTO_ADMIN_PREFIX"]
