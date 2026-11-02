"""Native sidecars must load and hold tasks without another channel installed."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SidecarDependencyTests(unittest.TestCase):
    def test_tt_only_release_imports_and_holds_pending_and_ready_tasks(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory)
            (release / "features").mkdir()
            (release / "features" / "__init__.py").write_text("")
            shutil.copytree(root / "features" / "pipeline_dispatch",
                            release / "features" / "pipeline_dispatch",
                            ignore=shutil.ignore_patterns("__pycache__"))
            # The production TT release contains no FB package. This subprocess
            # cannot fall back to already-imported local FB test fixtures.
            script = r'''
import sqlite3, sys
from types import SimpleNamespace
from features.pipeline_dispatch.tt_native import TTNativeBridge
from features.pipeline_dispatch.tt_readback import reconcile_next
from features.pipeline_dispatch.adapters import AdapterError
assert not any(name.startswith("features.tt_auto_posts") for name in sys.modules)
db = "tt.sqlite3"
with sqlite3.connect(db) as c:
    c.execute("CREATE TABLE tt_auto_task(id INTEGER PRIMARY KEY,status TEXT,scheduled_at_utc TEXT,created_at TEXT,claim_token TEXT,claim_phase TEXT,publish_attempt_count INTEGER,unknown_outcome INTEGER,publish_id TEXT)")
    c.executemany("INSERT INTO tt_auto_task VALUES(?,?,?,?,?,?,?,?,?)", [
        (1, "pending", "2026-11-01T10:00:00+00:00", "2026-11-01T09:00:00+00:00", "", "", 0, 0, ""),
        (2, "ready", "2026-11-01T10:00:00+00:00", "2026-11-01T09:00:00+00:00", "", "", 0, 0, ""),
        (3, "pending", "2026-11-03T10:00:00+00:00", "2026-11-01T09:00:00+00:00", "", "", 0, 0, ""),
        (4, "ready", "2026-11-01T10:00:00+00:00", "2026-11-01T09:00:00+00:00", "", "", 1, 0, "")])
    before = c.execute("SELECT * FROM tt_auto_task ORDER BY id").fetchall()
bridge = TTNativeBridge(SimpleNamespace(store=SimpleNamespace(db_path=db)), automatic=True)
def pause(identity):
    return bridge.dispatch("pause", {"job": {"channel": "tt", "family": "auto", "source_key": "tt:auto:" + str(identity), "source_id": str(identity)}, "cutoff": "2026-11-02T11:29:52+00:00"})
for identity in (1, 2, 1):
    assert pause(identity)["held"] is True
for identity, error in ((3, "canonical_task_outside_cutoff"), (4, "canonical_task_has_publish_evidence")):
    try:
        pause(identity)
    except AdapterError as exc:
        assert exc.code == error, exc.code
    else:
        raise AssertionError("ineligible task held")
with sqlite3.connect(db) as c:
    assert c.execute("SELECT * FROM tt_auto_task ORDER BY id").fetchall() == before
    assert c.execute("SELECT COUNT(*) FROM pipeline_canonical_hold").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM pipeline_tt_operation").fetchone()[0] == 0
assert not any(name.startswith(("features.fb_auto_posts", "features.fb_video_publish")) for name in sys.modules)
print("tt_only_pause_ok")
'''
            result = subprocess.run([sys.executable, "-c", script], cwd=release,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "tt_only_pause_ok")


if __name__ == "__main__":
    unittest.main()
