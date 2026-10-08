"""systemd uses calendar-size units after long host uptime."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "features/tt_auto_posts/automation_health.py"
spec = importlib.util.spec_from_file_location("automation_health", PATH)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class TimespanTests(unittest.TestCase):
    def test_actual_two_month_systemd_value(self):
        self.assertAlmostEqual(
            health.monotonic_seconds("2month 1d 5h 52min 30.827827s"),
            2 * 2629800 + 86400 + 5 * 3600 + 52 * 60 + 30.827827,
        )

    def test_year_week_and_subsecond_units(self):
        self.assertAlmostEqual(health.monotonic_seconds("1y 2w 3ms 4us"),
                               31557600 + 2 * 604800 + .003004)
        self.assertEqual(health.monotonic_seconds("3000000"), 3)

    def test_invalid_values_remain_fail_closed(self):
        for raw in ("2fortnight", "-3s", "2month garbage"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                health.monotonic_seconds(raw)

    def test_probe_handles_real_long_uptime_output(self):
        from types import SimpleNamespace
        blocks = []
        for name in health.TRIGGERS + health.SERVICES:
            blocks.append("Id=" + name + "\nLoadState=loaded\nActiveState=active\n"
                          "LastTriggerUSecMonotonic=2month 1d 5h 52min 30.827827s")
        result = SimpleNamespace(returncode=0, stdout="\n\n".join(blocks))
        now = health.monotonic_seconds("2month 1d 5h 52min 31s")
        self.assertTrue(health.probe_automation(run=lambda *a, **k: result,
                                              monotonic=lambda: now)["ready"])
        stale = health.probe_automation(run=lambda *a, **k: result,
                                       monotonic=lambda: now + 181)
        self.assertIn("tt-auto-post-scheduler.timer:not_firing", stale["problems"])


if __name__ == "__main__":
    unittest.main()
