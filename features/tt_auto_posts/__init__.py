"""Independent TikTok automatic publishing templates.

This package intentionally does not import or instantiate the legacy
``TTPostStore``.  Normal selection remains read-only against the legacy
publisher.  The separately sandboxed code-route broker is the only write
exception and may mutate only the shared four-character route tables.
"""

from .client import TT_AUTO_ADMIN_PREFIX

__all__ = ["TT_AUTO_ADMIN_PREFIX"]
