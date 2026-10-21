"""Deploy explicit-failure token rotation without changing tasks or links."""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

_source = Path(__file__).with_name("deploy_fb_page_list_performance.py")
_spec = importlib.util.spec_from_file_location("_fb_token_failover_delivery", _source)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)
release.BASELINE = "d57a4d6"
release.REMOTE_REF = "refs/heads/codex/fb-video-token-failover-20261021"
release.BACKEND_FILES = {"features/fb_video_publish/core.py", "features/fb_video_publish/worker.py",
                         "features/fb_auto_posts/publisher.py"}
release.STATIC_FILES = set()
release.ALLOWED = release.BACKEND_FILES


def destinations(relative):
    # The main API does not load GraphTransport and has a different historical
    # publisher baseline. Never replace that unrelated copy.
    roots = [release.base.FB] if relative == "features/fb_auto_posts/publisher.py" else [release.base.MAIN, release.base.FB]
    return [root / relative for root in roots]


def stage_targets():
    base, root = release.base, release.ROOT
    changed = base.run("git", "-C", str(root), "diff", "--name-only", release.BASELINE, "HEAD").splitlines()
    targets = []
    for relative in changed:
        if relative not in release.ALLOWED:
            base.require(not relative.startswith(("features/", "static/", "deploy/")) and relative != "app.py",
                         "Runtime file outside token failover patch: " + relative)
            continue
        source = root / relative
        compile(source.read_bytes(), str(source), "exec")
        expected = subprocess.check_output(["git", "-C", str(root), "show", release.BASELINE + ":" + relative])
        digest = hashlib.sha256(expected).hexdigest()
        for target in destinations(relative):
            base.require(base.sha(target) == digest, "Live baseline drift: " + str(target))
            targets.append((source, target, target.stat().st_mode & 0o777))
    base.require(len(targets) == 5, "Expected exactly five runtime copies")
    return targets


release.stage_targets = stage_targets
release.allowed_targets = lambda: {str(target) for relative in release.ALLOWED for target in destinations(relative)}

if __name__ == "__main__":
    release.main()
