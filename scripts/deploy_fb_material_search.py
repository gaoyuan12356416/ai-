"""Deploy only indexed material lookup and picker UX; preserve publisher facts."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_source = Path(__file__).with_name("deploy_fb_page_list_performance.py")
_spec = importlib.util.spec_from_file_location("_fb_material_search_delivery", _source)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Cannot load verified deployment flow")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)
release.BASELINE = "3e2aee6"
release.REMOTE_REF = "refs/heads/codex/fb-material-search-20261021"
release.BACKEND_FILES = {"features/fb_video_publish/sources.py"}
release.ALLOWED = release.BACKEND_FILES | release.STATIC_FILES

if __name__ == "__main__":
    release.main()
