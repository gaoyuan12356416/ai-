"""A failed candidate health check must restore the running release and wakeups."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import deploy_publish_queue_isolation as switcher


class SwitchRecoveryTests(unittest.TestCase):
    def test_failed_candidate_health_restores_old_runtime_and_scheduler(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            old, candidate, link = (directory / name for name in ("old", "candidate", "current"))
            old.mkdir()
            candidate.mkdir()
            unit, timer, port = "test-publisher.service", "test-planner.timer", 18899
            state = {"root": old, "running": True}
            commands, swaps = [], []
            manifest = {"lanes": {"x": {
                "before_root": str(old), "candidate": str(candidate),
                "files": {}, "state": "prepared",
            }}}
            switcher.save(directory / "manifest.json", manifest)
            original_resolve = Path.resolve

            def resolve(path, *args, **kwargs):
                return state["root"] if path == link else original_resolve(path, *args, **kwargs)

            def swap(path, target):
                self.assertEqual(path, link)
                swaps.append(target)
                state["root"] = target

            def run(args, **kwargs):
                args = list(map(str, args))
                commands.append(args)
                if args[:2] == ["systemctl", "show"]:
                    prop = args[args.index("-p") + 1]
                    return {"User": "test", "RequiredBy": "test-planner.service",
                            "TriggeredBy": timer, "ActiveState": "active"}.get(prop, "")
                if args[:2] == ["systemctl", "is-active"]:
                    return "active"
                if args[:3] == ["systemctl", "--job-mode=ignore-dependencies", "stop"] and unit in args:
                    state["running"] = False
                if args[:2] == ["systemctl", "start"] and unit in args:
                    state["running"] = True
                return ""

            def fail_candidate_health(requested_port, maintenance_triggers=()):
                self.assertEqual(requested_port, port)
                self.assertEqual(set(maintenance_triggers), {timer})
                self.assertEqual(state["root"], candidate)
                self.assertTrue(state["running"])
                raise RuntimeError("candidate_health_failed")

            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(switcher, "LANES", {"x": (str(link), [(unit, port, 1)])}))
                stack.enter_context(patch.object(Path, "resolve", resolve))
                stack.enter_context(patch.object(switcher, "swap", side_effect=swap))
                stack.enter_context(patch.object(switcher, "run", side_effect=run))
                stack.enter_context(patch.object(switcher, "pid", side_effect=lambda _: "99" if state["running"] else "0"))
                stack.enter_context(patch.object(switcher, "handlers", return_value=[1, 0, 0]))
                stack.enter_context(patch.object(switcher, "health", side_effect=fail_candidate_health))
                stack.enter_context(patch.object(switcher.subprocess, "run"))
                stack.enter_context(patch.dict(switcher.os.environ, {"QUEUE_ISOLATION_DRAIN_SECONDS": "20"}))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                with self.assertRaisesRegex(RuntimeError, "candidate_health_failed"):
                    switcher.switch(directory, "x")

            self.assertEqual(swaps, [candidate, old])
            self.assertEqual(state, {"root": old, "running": True})
            self.assertIn(["systemctl", "stop", timer], commands)
            self.assertIn(["systemctl", "start", timer], commands)
            self.assertEqual(sum(command[:3] == ["iptables", "-I", "INPUT"] for command in commands), 1)
            self.assertEqual(sum(command[:3] == ["iptables", "-D", "INPUT"] for command in commands), 1)
            self.assertNotEqual(json.loads((directory / "manifest.json").read_text())["lanes"]["x"]["state"], "active")
            self.assertTrue(json.loads((directory / "x-drain.json").read_text())["temporary_rules_removed"])


class MaintenanceHealthTests(unittest.TestCase):
    def unavailable(self, problems):
        body = json.dumps({"automation": {"problems": problems}}).encode()
        error = HTTPError("http://127.0.0.1:18899/health", 503, "unavailable", {}, io.BytesIO(body))
        self.addCleanup(error.close)
        return error

    def test_only_expected_paused_trigger_failures_pass_maintenance_probe(self):
        triggers = ("test-planner.timer", "test-publish.path")
        with patch.object(switcher.urllib.request, "build_opener") as build, \
                patch.object(switcher.time, "monotonic", return_value=0), \
                patch.object(switcher.time, "sleep") as sleep:
            build.return_value.open.side_effect = self.unavailable([name + ":inactive" for name in triggers])
            self.assertIsNone(switcher.health(18899, maintenance_triggers=triggers))
            build.return_value.open.assert_called_once()
            sleep.assert_not_called()

    def test_other_503_failures_and_normal_probe_never_pass(self):
        expected = "test-planner.timer:inactive"
        cases = [
            ([expected, "database_unavailable"], ("test-planner.timer",)),
            (["other-planner.timer:inactive"], ("test-planner.timer",)),
            (["test-planner.timer:failed"], ("test-planner.timer",)),
            ([], ("test-planner.timer",)),
            ([expected], ()),
        ]
        for problems, triggers in cases:
            with self.subTest(problems=problems, maintenance_triggers=triggers), \
                    patch.object(switcher.urllib.request, "build_opener") as build, \
                    patch.object(switcher.time, "monotonic", side_effect=[0, 16]), \
                    patch.object(switcher.time, "sleep") as sleep:
                build.return_value.open.side_effect = self.unavailable(problems)
                with self.assertRaisesRegex(RuntimeError, "health_failed:18899"):
                    switcher.health(18899, maintenance_triggers=triggers)
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
