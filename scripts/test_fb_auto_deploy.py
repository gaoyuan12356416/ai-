import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeployContractTests(unittest.TestCase):
    def test_live_gate_defaults_closed_and_graph_version_is_explicit(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("FB_AUTO_POST_LIVE_ENABLED=0", env)
        self.assertIn("FB_GRAPH_API_VERSION=v22.0", env)
        post_path=next(line for line in env.splitlines() if line.startswith("FB_AUTO_POST_DB_PATH="))
        metric_path=next(line for line in env.splitlines() if line.startswith("FB_AUTO_METRIC_DB_PATH="))
        self.assertNotEqual(post_path.split("=",1)[1],metric_path.split("=",1)[1])

    def test_units_use_isolated_user_env_and_release(self):
        for path in (ROOT / "deploy").glob("fb-auto-post-*.service"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("User=fb-auto-post", text)
            self.assertIn("EnvironmentFile=/etc/fb-auto-post.env", text)
            self.assertIn("/opt/fb-auto-post/current", text)
        service = (ROOT / "deploy" / "fb-auto-post-service.service").read_text(encoding="utf-8")
        self.assertIn("RuntimeDirectory=fb-auto-post", service)
        env = (ROOT / "deploy" / "fb-auto-post.env.example").read_text(encoding="utf-8")
        self.assertIn("FB_AUTO_METRIC_LOCK_PATH=/run/fb-auto-post/metric.lock", env)

    def test_publish_and_reconcile_have_bounded_workers_and_long_runtime(self):
        for name in ("fb-auto-post-runner.service", "fb-auto-post-reconcile.service"):
            text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
            self.assertIn("--workers 4 --max-tasks 4 --lease-seconds 1200", text)
            self.assertIn("RuntimeMaxSec=1500", text)
        runner=(ROOT/"scripts"/"fb_auto_post_runner.py").read_text(encoding="utf-8")
        self.assertIn('"/internal/fb-auto-post/execute-next": 1300',runner)
        self.assertIn('"/internal/fb-auto-post/reconcile-next": 1300',runner)
        self.assertIn('parser.add_argument("--lease-seconds", type=int, default=1200)',runner)

    def test_scheduler_is_fast_and_heavy_work_has_separate_units(self):
        runner = (ROOT / "scripts" / "fb_auto_post_runner.py").read_text(encoding="utf-8")
        unit = (ROOT / "deploy" / "fb-auto-post-scheduler.service").read_text(encoding="utf-8")
        self.assertIn('choices=("tick", "plan", "prepare", "execute", "reconcile")', runner)
        self.assertIn("TimeoutStartSec=60", unit); self.assertIn("RuntimeMaxSec=60", unit)
        self.assertTrue((ROOT / "deploy" / "fb-auto-post-plan.service").exists())
        self.assertTrue((ROOT / "deploy" / "fb-auto-post-prepare.service").exists())

    def test_prepare_timeout_lease_and_unit_are_aligned(self):
        runner=(ROOT/"scripts"/"fb_auto_post_runner.py").read_text(encoding="utf-8"); unit=(ROOT/"deploy"/"fb-auto-post-prepare.service").read_text(encoding="utf-8")
        self.assertIn('"/internal/fb-auto-post/prepare-next": 9600',runner); self.assertIn("--workers 1 --max-tasks 1 --lease-seconds 10200",unit); self.assertIn("RuntimeMaxSec=10800",unit)

    def test_gpu_unit_points_to_versioned_repo_entrypoint(self):
        unit=(ROOT/"deploy"/"fb-page-random-overlay-gpu.service").read_text(encoding="utf-8")
        self.assertIn("/root/miniconda3/envs/drama-voice/bin/python", unit)
        self.assertIn("scripts/fb_random_overlay_gpu_worker.py",unit); self.assertTrue((ROOT/"scripts"/"fb_random_overlay_gpu_worker.py").exists())


if __name__ == "__main__": unittest.main()
