#!/usr/bin/env python3
"""Local fault-injection tests for maintenance pause/resume recovery."""
import ast
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


HERE = pathlib.Path(__file__).resolve().parent


def load_maintenance():
    spec = importlib.util.spec_from_file_location("maintenance_recovery_under_test",
                                                  str(HERE / "maintenance.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


maintenance = load_maintenance()


def load_drama_drain():
    spec = importlib.util.spec_from_file_location("drama_drain_contract_under_test",
                                                  str(HERE / "drama_drain.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drama_drain = load_drama_drain()


class Result(object):
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRuntime(object):
    def __init__(self, units, cron, events):
        self.units = dict(units)
        self.cron = cron
        self.events = events
        self.failures = {}
        self.attempts = {}
        self.failure_ordinals = {}

    def fail_next(self, key, count=1):
        self.failures[key] = self.failures.get(key, 0) + count

    def fail_on_attempt(self, key, ordinal):
        self.failure_ordinals.setdefault(key, set()).add(ordinal)

    def _fail(self, key):
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.attempts[key] in self.failure_ordinals.get(key, set()):
            self.events.append(("injected-failure", key))
            raise RuntimeError("injected failure: " + key)
        remaining = self.failures.get(key, 0)
        if remaining:
            self.failures[key] = remaining - 1
            self.events.append(("injected-failure", key))
            raise RuntimeError("injected failure: " + key)

    def __call__(self, args, check=True, input_text=None):
        command = tuple(args)
        if args[:2] == ["systemctl", "is-active"]:
            unit = args[2]
            key = "is-active:" + unit
            self.events.append(("command", key))
            self._fail(key)
            state = self.units[unit]
            return Result(0 if state in ("active", "activating") else 3,
                          state + "\n", "")
        if args[:2] == ["systemctl", "stop"]:
            unit = args[2]
            key = "stop:" + unit
            self.events.append(("mutation", key))
            self._fail(key)
            self.units[unit] = "inactive"
            return Result()
        if args[:2] == ["systemctl", "start"]:
            unit = args[2]
            key = "start:" + unit
            self.events.append(("mutation", key))
            self._fail(key)
            self.units[unit] = "active"
            return Result()
        if args == ["crontab", "-l"]:
            self.events.append(("command", "cron-read"))
            self._fail("cron-read")
            return Result(stdout=self.cron)
        if args == ["crontab", "-"]:
            self.events.append(("mutation", "cron-write"))
            self._fail("cron-write")
            self.cron = input_text
            return Result()
        if args == ["nginx", "-t"]:
            self.events.append(("command", "nginx-test"))
            self._fail("nginx-test")
            return Result()
        if args == ["systemctl", "reload", "nginx"]:
            self.events.append(("mutation", "reload-nginx"))
            self._fail("reload-nginx")
            return Result()
        raise AssertionError("unexpected command: %r" % (command,))


class AtomicController(object):
    def __init__(self, original, original_create, events, base, snapshot,
                 snapshot_dir, map_path, gate_path):
        self.original = original
        self.original_create = original_create
        self.events = events
        self.base = base
        self.snapshot = snapshot
        self.snapshot_dir = snapshot_dir
        self.map_path = map_path
        self.gate_path = gate_path
        self.failures = {}

    def fail_next(self, key, count=1):
        self.failures[key] = self.failures.get(key, 0) + count

    def _key(self, path, data):
        if path == self.snapshot:
            return "snapshot"
        if path.parent == self.snapshot_dir:
            return "cycle-snapshot"
        if path == self.map_path:
            return "map"
        if path == self.gate_path:
            return "gate"
        if path == self.base / "gates.json":
            return "gates"
        if path.name.endswith("-triggers.json"):
            try:
                phase = json.loads(data).get("phase", "legacy")
            except (TypeError, ValueError):
                phase = "invalid"
            return "journal:" + phase
        return "file:" + path.name

    def __call__(self, path, data, mode=0o600):
        path = pathlib.Path(path)
        key = self._key(path, data)
        self.events.append(("write-attempt", key))
        remaining = self.failures.get(key, 0)
        if remaining:
            self.failures[key] = remaining - 1
            self.events.append(("injected-failure", key))
            raise RuntimeError("injected atomic failure: " + key)
        self.original(path, data, mode)
        self.events.append(("write-complete", key))

    def create(self, path, data):
        path = pathlib.Path(path)
        key = self._key(path, data)
        self.events.append(("write-attempt", key))
        remaining = self.failures.get(key, 0)
        if remaining:
            self.failures[key] = remaining - 1
            self.events.append(("injected-failure", key))
            raise RuntimeError("injected create failure: " + key)
        self.original_create(path, data)
        self.events.append(("write-complete", key))


class MaintenanceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.data_root = self.root / "data"
        self.base = self.data_root / "migrations" / maintenance.RUN_ID / "control"
        self.snapshot = self.base / "materials-crontab-before.txt"
        self.snapshot_dir = self.base / "snapshots"
        self.control_lock = self.base / ".maintenance.lock"
        self.map_path = self.root / "nginx" / "map.conf"
        self.gate_path = self.root / "nginx" / "gate.conf"
        self.events = []
        self.original_cron = (
            "MAILTO=ops@example.invalid\n"
            "*/5 * * * * /root/run_auto_cover_synthesis.sh >> /tmp/cover.log 2>&1\n"
            "0 4 * * * /root/unrelated-maintenance.sh\n"
        )
        self.units = maintenance.TRIGGERS["materials"]
        self.runtime = FakeRuntime({self.units[0]: "active", self.units[1]: "inactive"},
                                   self.original_cron, self.events)
        self.real_atomic_write = maintenance.atomic_write
        self.real_create_private = maintenance.create_private_text
        self.atomic = AtomicController(self.real_atomic_write, self.real_create_private,
                                       self.events, self.base, self.snapshot,
                                       self.snapshot_dir, self.map_path, self.gate_path)

        @contextlib.contextmanager
        def test_transaction():
            self.base.mkdir(parents=True, exist_ok=True)
            yield

        def test_snapshot_directory():
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            return self.snapshot_dir

        def test_read_optional_private(path):
            path = pathlib.Path(path)
            if path.is_symlink():
                raise RuntimeError("unsafe test symlink")
            return path.read_text() if path.is_file() else None

        def test_read_private(path):
            value = test_read_optional_private(path)
            if value is None:
                raise RuntimeError("missing test private file")
            return value

        def test_read_config(path):
            path = pathlib.Path(path)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise RuntimeError("unsafe test config")
            return path.read_bytes() if path.is_file() else None

        def test_unlink(path, private=False):
            path = pathlib.Path(path)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise RuntimeError("unsafe test unlink")
            if path.exists():
                path.unlink()

        self.patchers = [
            mock.patch.object(maintenance, "DATA_ROOT", self.data_root),
            mock.patch.object(maintenance, "BASE", self.base),
            mock.patch.object(maintenance, "MATERIALS_CRON_SNAPSHOT", self.snapshot),
            mock.patch.object(maintenance, "SNAPSHOT_DIR", self.snapshot_dir),
            mock.patch.object(maintenance, "CONTROL_LOCK", self.control_lock),
            mock.patch.object(maintenance, "MAP", self.map_path),
            mock.patch.object(maintenance, "GATE", self.gate_path),
            mock.patch.object(maintenance, "run", self.runtime),
            mock.patch.object(maintenance, "atomic_write", self.atomic),
            mock.patch.object(maintenance, "create_private_text", self.atomic.create),
            mock.patch.object(maintenance, "control_transaction", test_transaction),
            mock.patch.object(maintenance, "secure_snapshot_directory",
                              test_snapshot_directory),
            mock.patch.object(maintenance, "require_root_identity", lambda: None),
            mock.patch.object(maintenance, "validate_private_file",
                              lambda path: pathlib.Path(path).stat()),
            mock.patch.object(maintenance, "read_private_text", test_read_private),
            mock.patch.object(maintenance, "read_optional_private_text",
                              test_read_optional_private),
            mock.patch.object(maintenance, "read_optional_regular_bytes", test_read_config),
            mock.patch.object(maintenance, "safe_unlink", test_unlink),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def call(self, function, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return function(*args)

    def journal(self):
        return json.loads((self.base / "materials-triggers.json").read_text())

    def mutation_count(self):
        return len([event for event in self.events if event[0] == "mutation"])

    def test_python36_grammar(self):
        source = (HERE / "maintenance.py").read_text(encoding="utf-8")
        if sys.version_info >= (3, 8):
            ast.parse(source, filename="maintenance.py", feature_version=6)
        else:
            ast.parse(source, filename="maintenance.py")

    def test_pause_is_exact_private_and_drama_contract_compatible(self):
        self.call(maintenance.pause, "materials")

        expected_paused = maintenance.paused_crontab(self.original_cron)
        state = self.journal()
        self.assertEqual(self.runtime.units,
                         {self.units[0]: "inactive", self.units[1]: "inactive"})
        self.assertEqual(self.runtime.cron, expected_paused)
        self.assertEqual(self.snapshot.read_text(), self.original_cron)
        self.assertEqual(set(state["original"]), set(self.units))
        self.assertEqual(state["original"][self.units[0]], "active")
        self.assertEqual(state["original"][self.units[1]], "inactive")
        self.assertIs(state["restored"], False)
        self.assertEqual(state["phase"], "paused")
        self.assertEqual(state["current"]["cron"], "paused")
        self.assertTrue(all(value == "inactive"
                            for value in state["current"]["units"].values()))
        cycle_snapshot = pathlib.Path(state["cron"]["snapshot_path"])
        self.assertEqual(cycle_snapshot.parent, self.snapshot_dir)
        self.assertNotEqual(cycle_snapshot, self.snapshot)
        self.assertEqual(cycle_snapshot.read_text(), self.original_cron)
        self.assertEqual(state["cron"]["compatibility_snapshot_path"],
                         str(self.snapshot))
        self.assertEqual(drama_drain.paused_crontab(self.snapshot.read_text(),
                                                    self.runtime.cron),
                         self.runtime.cron)
        cycle_done = self.events.index(("write-complete", "cycle-snapshot"))
        compatibility_done = self.events.index(("write-complete", "snapshot"))
        journal_done = self.events.index(("write-complete", "journal:prepared"))
        first_mutation = min(index for index, event in enumerate(self.events)
                             if event[0] == "mutation")
        self.assertLess(cycle_done, journal_done)
        self.assertLess(journal_done, first_mutation)
        self.assertLess(compatibility_done, first_mutation)

    def test_repeated_pause_and_resume_are_idempotent(self):
        self.call(maintenance.pause, "materials")
        after_pause = self.mutation_count()
        self.call(maintenance.pause, "materials")
        self.assertEqual(self.mutation_count(), after_pause)
        self.assertEqual(self.journal()["phase"], "paused")

        self.call(maintenance.resume, "materials")
        after_resume = self.mutation_count()
        self.assertEqual(self.runtime.units[self.units[0]], "active")
        self.assertEqual(self.runtime.units[self.units[1]], "inactive")
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertIs(self.journal()["restored"], True)
        self.call(maintenance.resume, "materials")
        self.assertEqual(self.mutation_count(), after_resume)

    def test_snapshot_or_prepared_journal_failure_precedes_all_mutation(self):
        self.atomic.fail_next("cycle-snapshot")
        with self.assertRaisesRegex(RuntimeError, "snapshot"):
            self.call(maintenance.pause, "materials")
        self.assertEqual(self.mutation_count(), 0)
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertFalse((self.base / "materials-triggers.json").exists())

    def test_new_cycle_prepared_failure_preserves_old_journal_and_snapshot(self):
        self.call(maintenance.pause, "materials")
        first_paused = self.journal()
        first_path = pathlib.Path(first_paused["cron"]["snapshot_path"])
        self.call(maintenance.resume, "materials")
        first_restored = self.journal()
        first_bytes = first_path.read_bytes()
        old_compatibility = self.snapshot.read_bytes()
        changed_cron = self.original_cron.replace("*/5", "*/7", 1)
        self.runtime.cron = changed_cron
        before_mutations = self.mutation_count()
        self.atomic.fail_next("journal:prepared")
        with self.assertRaisesRegex(RuntimeError, "journal:prepared"):
            self.call(maintenance.pause, "materials")
        self.assertEqual(self.journal(), first_restored)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(self.snapshot.read_bytes(), old_compatibility)
        self.assertEqual(self.mutation_count(), before_mutations)
        orphan_paths = sorted(self.snapshot_dir.glob("materials-crontab-*.txt"))
        self.assertEqual(len(orphan_paths), 2)

        self.call(maintenance.pause, "materials")
        second = self.journal()
        second_path = pathlib.Path(second["cron"]["snapshot_path"])
        self.assertNotEqual(second_path, first_path)
        self.assertEqual(second_path.read_text(), changed_cron)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(self.snapshot.read_text(), changed_cron)
        self.assertEqual(second["phase"], "paused")

    def test_cycle_snapshot_collision_never_overwrites(self):
        self.snapshot_dir.mkdir(parents=True)

        class FixedUuid(object):
            hex = "a" * 32

        target = self.snapshot_dir / ("materials-crontab-" + FixedUuid.hex + ".txt")
        target.write_text("old", encoding="utf-8")
        with mock.patch.object(maintenance.uuid, "uuid4", return_value=FixedUuid()):
            with self.assertRaisesRegex(RuntimeError, "unique materials cron snapshot"):
                maintenance.create_cycle_snapshot("new")
        self.assertEqual(target.read_text(), "old")

        self.atomic.fail_next("journal:prepared")
        with self.assertRaisesRegex(RuntimeError, "journal:prepared"):
            self.call(maintenance.pause, "materials")
        self.assertEqual(self.mutation_count(), 0)
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertFalse((self.base / "materials-triggers.json").exists())

    def test_pausing_journal_failure_rolls_back_without_mutation(self):
        self.atomic.fail_next("journal:pausing")
        with self.assertRaisesRegex(RuntimeError, "journal:pausing"):
            self.call(maintenance.pause, "materials")
        state = self.journal()
        self.assertEqual(self.mutation_count(), 0)
        self.assertEqual(state["phase"], "restored")
        self.assertIs(state["restored"], True)
        self.assertEqual(state["errors"][0]["action"], "pause")

    def test_cron_pause_failure_rolls_back_and_records_original_cause(self):
        self.runtime.fail_next("cron-write")
        with self.assertRaisesRegex(RuntimeError, "exact paused state"):
            self.call(maintenance.pause, "materials")
        state = self.journal()
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertEqual(self.runtime.units[self.units[0]], "active")
        self.assertIs(state["restored"], True)
        self.assertEqual(state["phase"], "restored")
        self.assertTrue(any(item["action"] == "pause-cron" for item in state["errors"]))

    def test_second_stop_failure_restores_first_service_and_cron(self):
        self.runtime.units[self.units[1]] = "active"
        self.runtime.fail_next("stop:" + self.units[1])
        with self.assertRaisesRegex(RuntimeError, "exact paused state"):
            self.call(maintenance.pause, "materials")
        state = self.journal()
        self.assertEqual(self.runtime.units,
                         {self.units[0]: "active", self.units[1]: "active"})
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertIs(state["restored"], True)
        self.assertTrue(any(item.get("unit") == self.units[1]
                            for item in state["errors"]))

    def test_first_stop_failure_restores_cron_without_changing_services(self):
        self.runtime.units[self.units[1]] = "active"
        self.runtime.fail_next("stop:" + self.units[0])
        with self.assertRaisesRegex(RuntimeError, "exact paused state"):
            self.call(maintenance.pause, "materials")
        state = self.journal()
        self.assertEqual(self.runtime.units,
                         {self.units[0]: "active", self.units[1]: "active"})
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertEqual(state["phase"], "restored")
        self.assertIs(state["restored"], True)

    def test_final_paused_journal_failure_rolls_back_external_state(self):
        self.atomic.fail_next("journal:paused")
        with self.assertRaisesRegex(RuntimeError, "journal:paused"):
            self.call(maintenance.pause, "materials")
        state = self.journal()
        self.assertEqual(self.runtime.units[self.units[0]], "active")
        self.assertEqual(self.runtime.units[self.units[1]], "inactive")
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertEqual(state["phase"], "restored")
        self.assertIs(state["restored"], True)

    def test_incomplete_pause_rollback_is_explicit_and_resume_converges(self):
        self.runtime.units[self.units[1]] = "active"
        self.runtime.fail_next("stop:" + self.units[1])
        self.runtime.fail_next("start:" + self.units[0])
        with self.assertRaisesRegex(RuntimeError, "rollback is incomplete"):
            self.call(maintenance.pause, "materials")
        state = self.journal()
        self.assertEqual(self.runtime.units[self.units[0]], "inactive")
        self.assertEqual(self.runtime.units[self.units[1]], "active")
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertEqual(state["phase"], "pause_rollback_incomplete")
        self.assertIs(state["restored"], False)

        self.call(maintenance.resume, "materials")
        state = self.journal()
        self.assertEqual(self.runtime.units[self.units[0]], "active")
        self.assertEqual(self.runtime.units[self.units[1]], "active")
        self.assertEqual(state["phase"], "restored")
        self.assertIs(state["restored"], True)
        self.assertTrue(any(item["phase"] == "pause_rollback_incomplete"
                            for item in state["history"]))

    def test_incomplete_pause_rollback_can_retry_pause_to_exact_state(self):
        self.runtime.units[self.units[1]] = "active"
        self.runtime.fail_next("stop:" + self.units[1])
        self.runtime.fail_next("start:" + self.units[0])
        with self.assertRaisesRegex(RuntimeError, "rollback is incomplete"):
            self.call(maintenance.pause, "materials")
        self.call(maintenance.pause, "materials")
        state = self.journal()
        self.assertEqual(self.runtime.units,
                         {self.units[0]: "inactive", self.units[1]: "inactive"})
        self.assertEqual(self.runtime.cron, maintenance.paused_crontab(self.original_cron))
        self.assertEqual(state["phase"], "paused")
        self.assertIs(state["restored"], False)

    def test_pause_rollback_cron_failure_is_explicit_and_resume_converges(self):
        self.runtime.units[self.units[1]] = "active"
        self.runtime.fail_next("stop:" + self.units[1])
        self.runtime.fail_on_attempt("cron-write", 2)
        with self.assertRaisesRegex(RuntimeError, "rollback is incomplete"):
            self.call(maintenance.pause, "materials")
        state = self.journal()
        self.assertEqual(self.runtime.units,
                         {self.units[0]: "active", self.units[1]: "active"})
        self.assertEqual(self.runtime.cron, maintenance.paused_crontab(self.original_cron))
        self.assertEqual(state["phase"], "pause_rollback_incomplete")
        self.assertTrue(any(item["action"] == "restore-cron" for item in state["errors"]))
        self.call(maintenance.resume, "materials")
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertIs(self.journal()["restored"], True)

    def test_resume_start_failure_is_retryable_and_preserves_inactive_unit(self):
        self.runtime.units[self.units[1]] = "active"
        self.call(maintenance.pause, "materials")
        self.runtime.fail_next("start:" + self.units[0])
        with self.assertRaisesRegex(RuntimeError, "resume is incomplete"):
            self.call(maintenance.resume, "materials")
        state = self.journal()
        self.assertEqual(state["phase"], "resume_incomplete")
        self.assertIs(state["restored"], False)
        self.assertEqual(self.runtime.units[self.units[0]], "inactive")
        self.assertEqual(self.runtime.units[self.units[1]], "active")
        self.assertEqual(self.runtime.cron, self.original_cron)

        self.call(maintenance.resume, "materials")
        self.assertEqual(self.runtime.units[self.units[0]], "active")
        self.assertEqual(self.runtime.units[self.units[1]], "active")
        self.assertIs(self.journal()["restored"], True)

    def test_resume_cron_failure_is_retryable(self):
        self.call(maintenance.pause, "materials")
        self.runtime.fail_next("cron-write")
        with self.assertRaisesRegex(RuntimeError, "resume is incomplete"):
            self.call(maintenance.resume, "materials")
        self.assertEqual(self.runtime.units[self.units[0]], "active")
        self.assertEqual(self.runtime.cron, maintenance.paused_crontab(self.original_cron))
        self.assertIs(self.journal()["restored"], False)

        self.call(maintenance.resume, "materials")
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertIs(self.journal()["restored"], True)

    def test_resume_refuses_cron_drift_until_exact_known_form_returns(self):
        self.call(maintenance.pause, "materials")
        expected_paused = maintenance.paused_crontab(self.original_cron)
        self.runtime.cron = expected_paused + "# operator-added-line\n"
        with self.assertRaisesRegex(RuntimeError, "resume is incomplete"):
            self.call(maintenance.resume, "materials")
        first = self.journal()
        self.assertEqual(first["phase"], "resume_incomplete")
        self.assertIs(first["restored"], False)
        self.assertEqual(self.runtime.cron, expected_paused + "# operator-added-line\n")
        self.assertTrue(any(item["error_type"] == "CronDrift" for item in first["errors"]))
        with self.assertRaisesRegex(RuntimeError, "resume is incomplete"):
            self.call(maintenance.resume, "materials")

        self.runtime.cron = expected_paused
        self.call(maintenance.resume, "materials")
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.assertIs(self.journal()["restored"], True)

    def test_final_restored_journal_failure_is_retryable(self):
        self.call(maintenance.pause, "materials")
        self.atomic.fail_next("journal:restored")
        with self.assertRaisesRegex(RuntimeError, "journal:restored"):
            self.call(maintenance.resume, "materials")
        state = self.journal()
        self.assertEqual(state["phase"], "resuming")
        self.assertIs(state["restored"], False)
        self.assertEqual(self.runtime.units[self.units[0]], "active")
        self.assertEqual(self.runtime.cron, self.original_cron)

        self.call(maintenance.resume, "materials")
        self.assertIs(self.journal()["restored"], True)

    def test_snapshot_tamper_blocks_resume_before_any_mutation(self):
        self.call(maintenance.pause, "materials")
        before_units = dict(self.runtime.units)
        before_cron = self.runtime.cron
        before_mutations = self.mutation_count()
        cycle_snapshot = pathlib.Path(self.journal()["cron"]["snapshot_path"])
        cycle_snapshot.write_text(self.original_cron + "# tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "snapshot changed"):
            self.call(maintenance.resume, "materials")
        self.assertEqual(self.runtime.units, before_units)
        self.assertEqual(self.runtime.cron, before_cron)
        self.assertEqual(self.mutation_count(), before_mutations)
        self.assertEqual(self.journal()["phase"], "paused")
        self.assertIs(self.journal()["restored"], False)

    def test_resuming_journal_failure_mutates_nothing_and_retry_converges(self):
        self.call(maintenance.pause, "materials")
        before_units = dict(self.runtime.units)
        before_cron = self.runtime.cron
        before_mutations = self.mutation_count()
        self.atomic.fail_next("journal:resuming")
        with self.assertRaisesRegex(RuntimeError, "journal:resuming"):
            self.call(maintenance.resume, "materials")
        self.assertEqual(self.runtime.units, before_units)
        self.assertEqual(self.runtime.cron, before_cron)
        self.assertEqual(self.mutation_count(), before_mutations)
        self.assertIs(self.journal()["restored"], False)
        self.call(maintenance.resume, "materials")
        self.assertIs(self.journal()["restored"], True)

    def test_pause_rollback_evidence_write_failure_keeps_cause_and_recovers(self):
        self.runtime.fail_next("cron-write")
        self.atomic.fail_next("journal:pause_rollback")
        with self.assertRaisesRegex(RuntimeError, "exact paused state"):
            self.call(maintenance.pause, "materials")
        state = self.journal()
        self.assertEqual(state["phase"], "restored")
        self.assertIs(state["restored"], True)
        actions = [item["action"] for item in state["errors"]]
        self.assertIn("pause", actions)
        self.assertIn("pause-cron", actions)
        self.assertIn("write-pause-rollback-journal", actions)

    def test_final_pause_rollback_journal_failure_preserves_cause_and_is_retryable(self):
        self.runtime.fail_next("cron-write")
        self.atomic.fail_next("journal:restored")
        with self.assertRaisesRegex(RuntimeError, "could not be recorded") as caught:
            self.call(maintenance.pause, "materials")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        state = self.journal()
        self.assertEqual(state["phase"], "pause_rollback")
        self.assertIs(state["restored"], False)
        self.assertEqual(self.runtime.cron, self.original_cron)
        self.call(maintenance.resume, "materials")
        self.assertIs(self.journal()["restored"], True)

    def test_unstable_original_service_state_is_rejected_before_mutation(self):
        self.runtime.units[self.units[0]] = "activating"
        with self.assertRaisesRegex(RuntimeError, "stable active or inactive"):
            self.call(maintenance.pause, "materials")
        self.assertEqual(self.mutation_count(), 0)
        self.assertFalse(self.snapshot.exists())
        self.assertFalse((self.base / "materials-triggers.json").exists())

    def test_invalid_journal_phase_or_restored_conflict_precedes_mutation(self):
        self.call(maintenance.pause, "materials")
        path = self.base / "materials-triggers.json"
        baseline = self.journal()
        before = self.mutation_count()
        invalid = dict(baseline)
        invalid["phase"] = "made-up"
        path.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "phase is invalid"):
            self.call(maintenance.pause, "materials")
        self.assertEqual(self.mutation_count(), before)

        conflict = dict(baseline)
        conflict["restored"] = True
        path.write_text(json.dumps(conflict), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "phase/restored"):
            self.call(maintenance.pause, "materials")
        self.assertEqual(self.mutation_count(), before)

    def test_gate_state_write_failure_never_reloads_and_restores_exact_files(self):
        self.map_path.parent.mkdir(parents=True)
        self.map_path.write_text("old-map\n", encoding="utf-8")
        self.gate_path.write_text("old-gate\n", encoding="utf-8")
        self.base.mkdir(parents=True)
        state_path = self.base / "gates.json"
        state_path.write_text(json.dumps({"groups": []}, indent=2), encoding="utf-8")
        self.atomic.fail_next("gates")
        with self.assertRaisesRegex(RuntimeError, "gates"):
            self.call(maintenance.gate, "materials", True)
        self.assertEqual(self.map_path.read_text(), "old-map\n")
        self.assertEqual(self.gate_path.read_text(), "old-gate\n")
        self.assertEqual(json.loads(state_path.read_text()), {"groups": []})
        self.assertNotIn(("mutation", "reload-nginx"), self.events)

    def test_gate_reload_failure_restores_disk_and_reloads_old_runtime(self):
        self.map_path.parent.mkdir(parents=True)
        self.map_path.write_text("old-map\n", encoding="utf-8")
        self.gate_path.write_text("old-gate\n", encoding="utf-8")
        self.base.mkdir(parents=True)
        state_path = self.base / "gates.json"
        state_path.write_text(json.dumps({"groups": []}, indent=2), encoding="utf-8")
        self.runtime.fail_next("reload-nginx")
        with self.assertRaisesRegex(RuntimeError, "reload-nginx"):
            self.call(maintenance.gate, "materials", True)
        self.assertEqual(self.map_path.read_text(), "old-map\n")
        self.assertEqual(self.gate_path.read_text(), "old-gate\n")
        self.assertEqual(json.loads(state_path.read_text()), {"groups": []})
        reloads = [event for event in self.events
                   if event == ("mutation", "reload-nginx")]
        self.assertEqual(len(reloads), 2)


class StatView(object):
    def __init__(self, source, mode=None, uid=None, gid=None):
        self.source = source
        self.st_mode = source.st_mode if mode is None else mode
        self.st_uid = source.st_uid if uid is None else uid
        self.st_gid = source.st_gid if gid is None else gid

    def __getattr__(self, name):
        return getattr(self.source, name)


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.data_root = self.root / "mnt-data-disk"
        self.base = self.data_root / "migrations" / maintenance.RUN_ID / "control"
        self.base.mkdir(parents=True)
        self.snapshot_dir = self.base / "snapshots"
        self.fixed_snapshot = self.base / "materials-crontab-before.txt"
        self.lock_path = self.base / ".maintenance.lock"
        self.map_path = self.root / "nginx" / "map.conf"
        self.gate_path = self.root / "nginx" / "gate.conf"
        self.map_path.parent.mkdir()
        self.real_lstat = os.lstat
        self.chain = [self.data_root, self.data_root / "migrations",
                      self.data_root / "migrations" / maintenance.RUN_ID, self.base]
        self.patchers = [
            mock.patch.object(maintenance, "DATA_ROOT", self.data_root),
            mock.patch.object(maintenance, "BASE", self.base),
            mock.patch.object(maintenance, "SNAPSHOT_DIR", self.snapshot_dir),
            mock.patch.object(maintenance, "MATERIALS_CRON_SNAPSHOT", self.fixed_snapshot),
            mock.patch.object(maintenance, "CONTROL_LOCK", self.lock_path),
            mock.patch.object(maintenance, "MAP", self.map_path),
            mock.patch.object(maintenance, "GATE", self.gate_path),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def trusted_lstat(self, path):
        value = self.real_lstat(path)
        candidate = pathlib.Path(path)
        if candidate in self.chain:
            bits = 0o700 if candidate == self.base else 0o755
            return StatView(value, stat.S_IFDIR | bits, uid=0, gid=0)
        return value

    def mount_run(self, args, check=True, input_text=None, target=None, uuid_value=None):
        if args[:4] != ["findmnt", "-n", "-o", "TARGET,UUID"]:
            raise AssertionError("unexpected secure-base command: %r" % (args,))
        selected_target = str(self.data_root) if target is None else target
        selected_uuid = maintenance.CPU_UUID if uuid_value is None else uuid_value
        return Result(stdout=selected_target + " " + selected_uuid + "\n")

    @contextlib.contextmanager
    def root_identity(self, euid=0, egid=0):
        with mock.patch.object(maintenance.os, "geteuid", return_value=euid, create=True), \
                mock.patch.object(maintenance.os, "getegid", return_value=egid, create=True):
            yield

    def call_secure_base(self, lstat_function=None, run_function=None):
        lstat_function = lstat_function or self.trusted_lstat
        run_function = run_function or self.mount_run
        with self.root_identity(), \
                mock.patch.object(maintenance.os, "lstat", side_effect=lstat_function), \
                mock.patch.object(maintenance.os, "access", return_value=True), \
                mock.patch.object(maintenance, "run", side_effect=run_function):
            return maintenance.secure_control_base(create=False)

    def test_non_root_identity_rejected_before_path_or_mount_probe(self):
        for euid, egid in ((1, 0), (0, 1)):
            lstat_probe = mock.Mock()
            run_probe = mock.Mock()
            with self.subTest(euid=euid, egid=egid), self.root_identity(euid, egid), \
                    mock.patch.object(maintenance.os, "lstat", lstat_probe), \
                    mock.patch.object(maintenance, "run", run_probe):
                with self.assertRaisesRegex(RuntimeError, "euid=egid=0"):
                    maintenance.secure_control_base(create=False)
            lstat_probe.assert_not_called()
            run_probe.assert_not_called()

    def test_every_control_parent_symlink_is_rejected(self):
        for target in self.chain:
            def linked_lstat(path, selected=target):
                value = self.trusted_lstat(path)
                if pathlib.Path(path) == selected:
                    return StatView(value, stat.S_IFLNK | 0o777, uid=0, gid=0)
                return value

            with self.subTest(target=str(target)):
                with self.assertRaisesRegex(RuntimeError, "unsafe control directory type"):
                    self.call_secure_base(lstat_function=linked_lstat)

    def test_cross_mount_and_resolved_escape_are_rejected(self):
        for field, value in (("target", str(self.root / "wrong-mount")),
                             ("uuid_value", "wrong-uuid")):
            def wrong_mount(args, check=True, input_text=None, key=field, wrong=value):
                values = {key: wrong}
                return self.mount_run(args, check, input_text, **values)

            with self.subTest(field=field), self.assertRaisesRegex(
                    RuntimeError, "expected CPU data disk"):
                self.call_secure_base(run_function=wrong_mount)

        real_resolve = pathlib.Path.resolve
        escaped = self.root / "mnt-data-disk-evil" / "control"

        def escape_base(path, strict=False):
            if path == self.base:
                return escaped
            return real_resolve(path, strict=strict)

        with mock.patch.object(pathlib.Path, "resolve", escape_base), \
                self.assertRaisesRegex(RuntimeError, "escaped the data root"):
            self.call_secure_base()

    def test_base_wrong_owner_mode_or_type_is_rejected(self):
        cases = ((stat.S_IFDIR | 0o700, 1, 0, "root:root"),
                 (stat.S_IFDIR | 0o700, 0, 1, "root:root"),
                 (stat.S_IFDIR | 0o755, 0, 0, "mode"),
                 (stat.S_IFREG | 0o600, 0, 0, "unsafe control directory type"))
        for mode, uid, gid, message in cases:
            def wrong_base(path, selected_mode=mode, selected_uid=uid, selected_gid=gid):
                value = self.trusted_lstat(path)
                if pathlib.Path(path) == self.base:
                    return StatView(value, selected_mode, selected_uid, selected_gid)
                return value

            with self.subTest(mode=oct(mode), uid=uid, gid=gid), \
                    self.assertRaisesRegex(RuntimeError, message):
                self.call_secure_base(lstat_function=wrong_base)

    def test_private_artifact_owner_mode_and_type_are_rejected_for_all_roles(self):
        source = self.real_lstat(self.base)
        artifacts = (self.base / "materials-triggers.json", self.fixed_snapshot,
                     self.base / "gates.json",
                     self.snapshot_dir / "materials-crontab-a.txt")
        cases = ((stat.S_IFREG | 0o600, 1, 0),
                 (stat.S_IFREG | 0o600, 0, 1),
                 (stat.S_IFREG | 0o644, 0, 0),
                 (stat.S_IFDIR | 0o700, 0, 0),
                 (stat.S_IFLNK | 0o777, 0, 0),
                 (stat.S_IFIFO | 0o600, 0, 0))
        for path in artifacts:
            for mode, uid, gid in cases:
                with self.subTest(path=path.name, mode=oct(mode), uid=uid, gid=gid), \
                        self.root_identity(), \
                        mock.patch.object(maintenance.os, "lstat",
                                          return_value=StatView(source, mode, uid, gid)), \
                        self.assertRaisesRegex(RuntimeError,
                                               "regular file|root:root mode 0600"):
                    maintenance.validate_private_file(path)

    def test_map_and_gate_symlink_or_non_regular_target_are_rejected(self):
        source = self.real_lstat(self.base)
        cases = ((stat.S_IFLNK | 0o777, 0, 0),
                 (stat.S_IFDIR | 0o755, 0, 0),
                 (stat.S_IFIFO | 0o644, 0, 0),
                 (stat.S_IFREG | 0o644, 1, 0),
                 (stat.S_IFREG | 0o600, 0, 0))
        for path in (self.map_path, self.gate_path):
            for mode, uid, gid in cases:
                with self.subTest(path=path.name, mode=oct(mode)), self.root_identity(), \
                        mock.patch.object(maintenance.os, "lstat",
                                          return_value=StatView(source, mode, uid, gid)), \
                        self.assertRaisesRegex(RuntimeError,
                                               "regular file|root:root mode 0644"):
                    maintenance.read_optional_regular_bytes(path)

    def test_unlink_fsyncs_parent_directory(self):
        self.map_path.write_text("config", encoding="utf-8")
        sync = mock.Mock()
        with self.root_identity(), \
                mock.patch.object(maintenance, "validate_regular_file", lambda path, value: None), \
                mock.patch.object(maintenance, "fsync_directory", sync):
            maintenance.safe_unlink(self.map_path)
        self.assertFalse(self.map_path.exists())
        sync.assert_called_once_with(self.map_path.parent)

    def test_control_lock_is_nonblocking_and_released(self):
        class FakeFcntl(object):
            LOCK_EX = 1
            LOCK_NB = 2
            LOCK_UN = 4

            def __init__(self, fail=False):
                self.fail = fail
                self.calls = []

            def flock(self, fd, operation):
                self.calls.append((fd, operation))
                if self.fail and operation == self.LOCK_EX | self.LOCK_NB:
                    raise OSError("busy")

        opened_stat = StatView(self.real_lstat(self.base), stat.S_IFREG | 0o600, 0, 0)
        fake = FakeFcntl()
        close = mock.Mock()
        with mock.patch.object(maintenance, "fcntl", fake), \
                mock.patch.object(maintenance, "require_root_identity", lambda: None), \
                mock.patch.object(maintenance, "validate_private_stat", lambda path, value: None), \
                mock.patch.object(maintenance, "fsync_directory", lambda path: None), \
                mock.patch.object(maintenance.os, "open", return_value=91), \
                mock.patch.object(maintenance.os, "fstat", return_value=opened_stat), \
                mock.patch.object(maintenance.os, "close", close):
            with maintenance.control_lock():
                pass
        self.assertEqual(fake.calls, [(91, fake.LOCK_EX | fake.LOCK_NB),
                                      (91, fake.LOCK_UN)])
        close.assert_called_once_with(91)

        busy = FakeFcntl(fail=True)
        with mock.patch.object(maintenance, "fcntl", busy), \
                mock.patch.object(maintenance, "require_root_identity", lambda: None), \
                mock.patch.object(maintenance, "validate_private_stat", lambda path, value: None), \
                mock.patch.object(maintenance, "fsync_directory", lambda path: None), \
                mock.patch.object(maintenance.os, "open", return_value=92), \
                mock.patch.object(maintenance.os, "fstat", return_value=opened_stat), \
                mock.patch.object(maintenance.os, "close"):
            with self.assertRaisesRegex(RuntimeError, "another maintenance"):
                with maintenance.control_lock():
                    pass


if __name__ == "__main__":
    unittest.main()
