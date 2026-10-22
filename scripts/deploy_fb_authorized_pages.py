"""Deploy only the shared authorized-Page source; preserve ledgers and workers."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_source = Path(__file__).with_name("deploy_fb_page_list_performance.py")
_spec = importlib.util.spec_from_file_location("_fb_authorized_pages_delivery", _source)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Cannot load verified deployment flow")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)
release.BASELINE = "429932cfdcbfc69d5544835e853285216bce5eae"
release.REMOTE_REF = "refs/heads/codex/fb-authorized-page-search-20261022"
release.BACKEND_FILES = {"features/fb_video_publish/sources.py"}
release.STATIC_FILES = set()
release.ALLOWED = release.BACKEND_FILES

_rollback = release.rollback


def rollback(manifest_path):
    # The previous source cannot authorize newly admitted unpooled Pages.
    # Never strand accepted work by restoring that narrower admission rule.
    with release.base.database() as conn:
        pending = conn.execute("""SELECT COUNT(*) FROM fb_video_task
            WHERE status NOT IN ('published','failed','cancelled') OR unknown_outcome<>0""").fetchone()[0]
        enabled = conn.execute("SELECT COUNT(*) FROM fb_video_pool WHERE status='enabled'").fetchone()[0]
    release.base.require(not pending and not enabled,
                         "Rollback requires pending/unknown tasks and enabled pools to be reconciled first; keep current code and database")
    return _rollback(manifest_path)


release.rollback = rollback

if __name__ == "__main__":
    release.main()
