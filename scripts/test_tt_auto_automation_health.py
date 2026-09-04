import subprocess
import unittest
from types import SimpleNamespace
from features.tt_auto_posts.automation_health import probe_automation, monotonic_seconds, TRIGGERS, SERVICES


class AutomationHealthTests(unittest.TestCase):
    def probe(self, changes=None, last=990000000):
        changes = changes or {}
        blocks = []
        for name in TRIGGERS + SERVICES:
            active = "active" if name in TRIGGERS else "inactive"
            state = {"Id": name, "LoadState": "loaded", "ActiveState": active,
                     "LastTriggerUSecMonotonic": str(last)}
            state.update(changes.get(name, {}))
            blocks.append("\n".join(k + "=" + v for k, v in state.items()))
        def run(command, **kwargs):
            self.assertEqual(command[1], "show")
            self.assertNotIn("start", command)
            return SimpleNamespace(returncode=0, stdout="\n\n".join(blocks))
        return probe_automation(run=run, monotonic=lambda: 1000)

    def test_healthy_with_inactive_oneshot_services(self):
        self.assertTrue(self.probe()["ready"])

    def test_production_systemd_timespan(self):
        self.assertAlmostEqual(monotonic_seconds("3w 3d 3h 18min 31.551189s"), 2085511.551189)
        self.assertTrue(self.probe(last="16min 30s")["ready"])

    def test_maintenance_stopped_timers_are_unhealthy(self):
        result = self.probe({name: {"ActiveState": "inactive"} for name in TRIGGERS})
        self.assertFalse(result["ready"])
        self.assertEqual(len(result["problems"]), 3)

    def test_missing_unit_is_unhealthy(self):
        self.assertFalse(self.probe({TRIGGERS[0]: {"LoadState": "not-found"}})["ready"])

    def test_failed_runner_is_unhealthy(self):
        self.assertFalse(self.probe({SERVICES[1]: {"ActiveState": "failed"}})["ready"])

    def test_stale_scheduler_is_unhealthy(self):
        self.assertIn(TRIGGERS[0] + ":not_firing", self.probe(last=1000000)["problems"])

    def test_long_render_does_not_false_alarm(self):
        self.assertTrue(self.probe({SERVICES[1]: {"ActiveState": "activating"}})["ready"])

    def test_timeout_and_missing_systemd_fail_closed(self):
        for error in (OSError(), subprocess.TimeoutExpired("systemctl", 3)):
            def run(*args, **kwargs):
                raise error
            self.assertFalse(probe_automation(run=run)["ready"])


if __name__ == "__main__":
    unittest.main()
