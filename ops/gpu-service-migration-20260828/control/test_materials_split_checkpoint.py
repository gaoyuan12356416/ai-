import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "materials_split_checkpoint", ROOT / "materials_split_checkpoint.py")
checkpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checkpoint)


def baseline_row(unit, pid, enabled="enabled"):
    return {"unit": unit, "active": "active", "substate": "running",
            "enabled": enabled, "pid": pid, "pid_start_ticks": pid + 100,
            "control_pid": 0, "control_group": "/system.slice/" + unit,
            "cgroup_pids": [pid], "nrestarts": 0,
            "start_monotonic": str(pid + 200),
            "active_enter_monotonic": str(pid + 300),
            "unit_sha256": "%064x" % pid}


def valid_envelope(now=1000.0):
    services = [baseline_row(unit, 100 + index)
                for index, unit in enumerate(checkpoint.AD_UNITS)]
    tunnel = baseline_row(checkpoint.AD_ONLY_TUNNEL, 200)
    states = []
    for index, unit in enumerate(checkpoint.IMAGE_SOURCE_UNITS):
        states.append({"unit": unit, "active": "active", "substate": "running",
                       "pid": 300 + index, "control_pid": 0,
                       "threads": 1, "children": []})
    for unit in checkpoint.LEGACY_TUNNELS:
        states.append({"unit": unit, "active": "inactive", "substate": "dead",
                       "pid": 0, "control_pid": 0})
    baseline = {"services": services, "tunnel": tunnel}
    idle = {}
    for row in services + [tunnel]:
        idle[row["unit"]] = {
            "unit": row["unit"], "active": "active", "substate": "running",
            "pid": row["pid"], "pid_start_ticks": row["pid_start_ticks"],
            "control_pid": 0, "control_group": row["control_group"],
            "cgroup_pids": [row["pid"]], "nrestarts": row["nrestarts"],
            "start_monotonic": row["start_monotonic"],
            "active_enter_monotonic": row["active_enter_monotonic"],
            "threads": 1, "children": []}
    return {"schema_version": 1, "run_id": checkpoint.RUN_ID,
            "source_host": checkpoint.US_HOST, "control_commit": "a" * 40,
            "checked_at_epoch": now,
            "source_fence_dry_run": {"dry_run": True, "group": "materials-images",
                                     "states": states, "us_ad_baseline": baseline},
            "health": {
                "8796": {"status": 200, "body": {"ok": True, "service": "ad-material-vision"}},
                "8797": {"status": 200, "body": {"ok": True, "service": "ad-material-generation"}}},
            "established_samples": [[], []], "ad_idle_samples": [idle, dict(idle)],
            "credentials_read": False, "service_mutations": False}


class MaterialsSplitCheckpointTests(unittest.TestCase):
    def test_valid_us_envelope_has_exact_split_scope(self):
        value = valid_envelope()
        self.assertIs(checkpoint.validate_us_envelope(value, "a" * 40, now=1050), value)

    def test_us_envelope_rejects_stale_or_active_legacy_tunnel(self):
        value = valid_envelope()
        with self.assertRaisesRegex(RuntimeError, "stale"):
            checkpoint.validate_us_envelope(value, "a" * 40, now=1201)
        value = valid_envelope()
        value["source_fence_dry_run"]["states"][-1].update(
            {"active": "active", "substate": "running", "pid": 999})
        with self.assertRaisesRegex(RuntimeError, "has not stopped"):
            checkpoint.validate_us_envelope(value, "a" * 40, now=1050)

    def test_us_envelope_rejects_ad_child_or_request(self):
        value = valid_envelope()
        unit = checkpoint.AD_UNITS[0]
        value["ad_idle_samples"][0][unit]["children"] = [777]
        value["ad_idle_samples"][1][unit]["children"] = [777]
        with self.assertRaisesRegex(RuntimeError, "idle identity"):
            checkpoint.validate_us_envelope(value, "a" * 40, now=1050)
        value = valid_envelope()
        value["established_samples"][1] = ["ESTAB sample"]
        with self.assertRaisesRegex(RuntimeError, "requests"):
            checkpoint.validate_us_envelope(value, "a" * 40, now=1050)

    def test_ad_baseline_requires_single_process_enabled_tunnel(self):
        value = valid_envelope()["source_fence_dry_run"]["us_ad_baseline"]
        value["services"][0]["cgroup_pids"].append(888)
        with self.assertRaisesRegex(RuntimeError, "child"):
            checkpoint.validate_ad_baseline(value)
        value = valid_envelope()["source_fence_dry_run"]["us_ad_baseline"]
        value["tunnel"]["enabled"] = "disabled"
        with self.assertRaisesRegex(RuntimeError, "not enabled"):
            checkpoint.validate_ad_baseline(value)

    def test_checkpoint_carries_exact_baseline_and_fail_closed_fields(self):
        envelope = valid_envelope()
        observation = {key: {} for key in (
            "gate", "status_counts", "paths", "image_units", "existing_image_units",
            "listeners", "ad_ssh_owner", "ad_health")}
        observation["affected_established"] = []
        result = checkpoint.build_checkpoint(envelope, observation, now=1060)
        self.assertTrue(result["ready"])
        self.assertEqual(result["split_mode"], "us-ad-only")
        self.assertIs(result["us_ad_baseline"],
                      envelope["source_fence_dry_run"]["us_ad_baseline"])
        self.assertTrue(result["legacy_shared_tunnel_stopped"])
        self.assertTrue(result["cpu_image_ports_owned_by_local_units"])
        self.assertFalse(result["credentials_read"])
        self.assertFalse(result["service_mutations"])

    def test_checkpoint_rejects_incomplete_cpu_observation(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            checkpoint.build_checkpoint(valid_envelope(), {"affected_established": []})


if __name__ == "__main__":
    unittest.main()
