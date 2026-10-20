"""Scoped pool-dialog patch using the verified, unchanged Page-list deploy flow.

Load an isolated module instance so its baseline and allowlist cannot mutate the
previous rollout's deploy or rollback command. Backups use the established
page-list-* directory convention; each manifest records this rollout's exact
baseline and GitHub commit. No database, schema, gate, app.py, or GPU code changes.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_source = Path(__file__).with_name("deploy_fb_page_list_performance.py")
_spec = importlib.util.spec_from_file_location("_fb_pool_load_performance_delivery", _source)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Cannot load the verified performance deployment flow")
_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_release)

_release.BASELINE = "0770d15"
_release.REMOTE_REF = "refs/heads/codex/fb-pool-load-performance-20261020"
_release.BACKEND_FILES = {"features/fb_video_publish/gateway.py"}
_release.ALLOWED = _release.BACKEND_FILES | _release.STATIC_FILES


def main():
    _release.main()


if __name__ == "__main__":
    main()
