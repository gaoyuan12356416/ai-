"""Rollback must not replay an account cleanup as global Video deletion."""
from pathlib import Path
import io
import json
from contextlib import redirect_stdout
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from deploy_meta_video_account_delete import guard_video_rollback, rollout
finally:
    sys.path.pop(0)


class AccountRollbackTests(unittest.TestCase):
    def guarded_client(self):
        source = (ROOT / "features/fb_ad_asset_delete/graph.py").read_text(encoding="utf-8")
        module = types.ModuleType("features.fb_ad_asset_delete.rollback_verification")
        module.__package__ = "features.fb_ad_asset_delete"
        exec(compile(guard_video_rollback(source), "rollback_verification.py", "exec"), module.__dict__)
        client = module.GraphClient(lambda users: "fake-secret")
        client.request = Mock(return_value=True)
        client.credential = lambda obj: "fake-secret"
        client.credential_context = lambda obj: {}
        return module, client

    def test_rollback_video_guard_prevents_all_old_node_requests(self):
        module, client = self.guarded_client()
        with self.assertRaises(module.GraphError) as caught:
            client.delete({"kind": "video", "object_id": "301", "result": {"delete_mode": "ad_account_video"}})
        self.assertEqual(caught.exception.code, "video_execution_rolled_back")
        client.request.assert_not_called()

    def test_guard_preserves_ad_deletion(self):
        _, client = self.guarded_client()
        state, result = client.delete({"kind": "ad", "object_id": "101"})
        self.assertEqual(state, "deleted")
        client.request.assert_called_once_with("DELETE", "101", "fake-secret")

    def test_guard_refuses_an_unrecognized_adapter(self):
        with self.assertRaises(AssertionError):
            guard_video_rollback("class Different: pass\n")

    def test_reapply_accepts_only_the_recorded_guard_after_rollback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            backup, release, target = root / "backup", root / "release", root / "live.py"
            backup.mkdir(); release.mkdir()
            (release / "graph.py").write_text("new")
            (backup / "graph.py").write_text("old")
            (backup / "guard.py").write_text("guard")
            target.write_text("new")
            item = dict(source="graph.py", target=str(target), saved="graph.py", rollback_saved="guard.py",
                before=rollout.digest(backup / "graph.py"), after=rollout.digest(release / "graph.py"),
                rollback_after=rollout.digest(backup / "guard.py"))
            plan = dict(commit="mock", release=str(release), backup=str(backup), files=[item])
            (backup / "plan.json").write_text(json.dumps(plan))
            (backup / "hashes.json").write_text(json.dumps({name: rollout.digest(backup / name)
                for name in ("graph.py", "guard.py", "plan.json")}))
            with patch.object(rollout, "ROOT", root), patch.object(rollout, "validate_disk"), patch.object(rollout, "idle"), redirect_stdout(io.StringIO()):
                rollout.switch(backup, rollback=True)
                self.assertEqual(target.read_text(), "guard")
                rollout.switch(backup)
                self.assertEqual(target.read_text(), "new")
                target.write_text("unrecognized-change")
                with self.assertRaisesRegex(AssertionError, "live drift"):
                    rollout.switch(backup)


if __name__ == "__main__":
    unittest.main()
