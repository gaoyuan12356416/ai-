"""Scoped manual/Excel blacklist waiver; preserve pool/automatic policy and facts."""
from pathlib import Path
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "_manual_blacklist_release", Path(__file__).with_name("deploy_fb_page_list_performance.py"))
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)
release.BASELINE = "fd25d7e"
release.REMOTE_REF = "refs/heads/codex/fb-manual-blacklist-20261022"
release.BACKEND_FILES = {"features/fb_video_publish/" + name + ".py"
                         for name in ("sources", "service", "excel", "worker")}
release.STATIC_FILES = set()
release.ALLOWED = release.BACKEND_FILES
_rollback = release.rollback


def require_no_pending_manual():
    with release.base.database() as conn:
        pending = conn.execute("""SELECT COUNT(*) FROM fb_video_task
            WHERE source_kind='manual' AND
            (status NOT IN ('published','failed','cancelled') OR unknown_outcome<>0)""").fetchone()[0]
    release.base.require(not pending, "Reconcile pending manual tasks before restoring the blacklist; keep current database")


def rollback(manifest_path):
    require_no_pending_manual()
    original_quiesce = release.pool_release.quiesce
    def quiesce_and_check(saved):
        original_quiesce(saved)
        require_no_pending_manual()
    release.pool_release.quiesce = quiesce_and_check
    try:
        return _rollback(manifest_path)
    finally:
        release.pool_release.quiesce = original_quiesce


release.rollback = rollback
if __name__ == "__main__":
    release.main()
