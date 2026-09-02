import importlib.util
import base64
import contextlib
import hashlib
import io
import json
import pathlib
import re
import sqlite3
import stat
import unittest
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent))


def load(name):
    p = pathlib.Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, str(p))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


maintenance = load("maintenance")
fence = load("source_fence")
permissions = load("hk_tunnel_permissions")
receiver = load("receive_archive")
finalize = load("finalize_config")
revoke = load("revoke_receiver")
drama = load("drama_drain")


def archived_capability():
    return {"verified": True, "definition_mode": "archived", "restorable": True,
            "can_retire_local": True, "can_mask_if_still_absent": False,
            "error_type": None}


def failed_capability(error):
    return {"verified": False, "definition_mode": None, "restorable": False,
            "can_retire_local": False, "can_mask_if_still_absent": False,
            "error_type": error}


def complete_shared_tunnel(**changes):
    state = {"unit": fence.DRAMA_SHARED_TUNNEL, "active": "active", "substate": "running",
             "enabled": "enabled", "pid": 456, "pid_start_ticks": 9001,
             "control_pid": 0,
             "control_group": "/system.slice/gpu-worker-reverse-tunnel.service",
             "cgroup_pids": [456], "nrestarts": 0, "start_monotonic": "100",
             "active_enter_monotonic": "101", "unit_sha256": "a" * 64}
    state.update(changes)
    return state


def unit_path_snapshot_for_roots(*roots):
    records = []
    for root in roots:
        identity = root.stat()
        records.append({"path": str(root), "kind": "directory",
                        "lstat": {"device": identity.st_dev, "inode": identity.st_ino,
                                  "mode": identity.st_mode,
                                  "uid": getattr(identity, "st_uid", 0),
                                  "gid": getattr(identity, "st_gid", 0)}})
    values = [str(root) for root in roots]
    return {"schema_version": 1, "manager": values, "analyzed": values,
            "roots": records}


def synthetic_link_record(path, target):
    raw_target = str(target)
    return {"path": str(path), "kind": "dangling-symlink",
            "link_target": raw_target,
            "link_target_sha256": hashlib.sha256(raw_target.encode("utf-8")).hexdigest(),
            "target_path": raw_target, "resolved_path": raw_target,
            "lstat": {"device": 1, "inode": len(str(path)), "mode": 41471,
                      "size": len(raw_target), "mtime_ns": 3}}


def synthetic_parent_resolution(target):
    path = pathlib.Path(target)
    return {"schema_version": 1, "kind": "resolved",
            "lexical_parent": str(path.parent),
            "canonical_parent": str(path.parent),
            "canonical_leaf_path": str(path), "directory_symlinks": []}


def fixture_absolute_path(*parts):
    return pathlib.Path(pathlib.Path.cwd().anchor).joinpath(
        "codex-source-fence-fixture", *parts)


def loaded_absent_definition():
    target = str(fence.DRAMA_LOCAL_FRAGMENT)
    link_target = target
    loaded = {"load_state": "loaded", "id": fence.DRAMA_UNIT,
              "names": [fence.DRAMA_UNIT], "fragment_path": target,
              "drop_in_paths": [], "active_state": "active", "sub_state": "running",
              "unit_file_state": "enabled", "main_pid": 123, "control_pid": 0,
              "control_group": "/system.slice/" + fence.DRAMA_UNIT,
              "exec_main_start_monotonic": "100", "active_enter_monotonic": "101",
              "nrestarts": 0, "pid_start_ticks": 8001, "cgroup_pids": [123],
              "threads": 1, "children": []}
    link = {"path": str(fence.DRAMA_WANTS_LINK), "kind": "dangling-symlink",
            "link_target": link_target,
            "link_target_sha256": hashlib.sha256(link_target.encode("utf-8")).hexdigest(),
            "target_path": target,
            "resolved_path": target,
            "lstat": {"device": 1, "inode": 2, "mode": 41471, "size": len(link_target),
                      "mtime_ns": 3}}
    link["target_parent_resolution"] = synthetic_parent_resolution(target)
    hop = dict(link)
    link.update({"chain_schema_version": 1, "chain": [hop], "terminal_path": target})
    unit_paths = unit_path_snapshot_for_roots(pathlib.Path.cwd())
    fragment = {"schema_version": 1, "path": target,
                "kind": "loaded_fragment_absent", "content_archived": False,
                 "restorable": False, "can_retire_local": False,
                 "can_mask_if_still_absent": True, "loaded_unit": loaded,
                 "systemd_unit_paths": unit_paths, "enablement_links": [link]}
    definition = {"fragment": fragment, "dropins": [], "schema_version": 1,
                  "definition_mode": "loaded_fragment_absent", "unit": fence.DRAMA_UNIT,
                  "restorable": False}
    definition["definition_sha256"] = hashlib.sha256(json.dumps(
        definition, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return definition


def absent_capability():
    return {"verified": True, "definition_mode": "loaded_fragment_absent",
            "restorable": False, "can_retire_local": False,
            "can_mask_if_still_absent": True, "error_type": None}


class ControlTests(unittest.TestCase):
    def test_only_selected_write_routes_are_gated(self):
        patterns = [re.compile(r"^(POST|PUT|PATCH|DELETE):" + p)
                    for p in maintenance.PATTERNS.values()]
        for route in ["POST:/api/admin/tt-posts/materials/preview",
                      "PUT:/api/admin/tt-auto-publish/templates/1",
                      "POST:/api/drama-screenshot-material/jobs",
                      "PATCH:/api/ad-material/tasks/1",
                      "DELETE:/api/admin/x-posts/material-pool/1"]:
            self.assertTrue(any(p.search(route) for p in patterns), route)
        for route in ["GET:/api/admin/tt-posts/queues", "POST:/api/gpu-video/cover",
                      "POST:/api/admin/x-accounts/1/verify", "GET:/tt",
                      "POST:/api/ad-material-other/jobs",
                      "POST:/api/drama-material/jobs"]:
            self.assertFalse(any(p.search(route) for p in patterns), route)

    def test_group_gate_retains_other_groups(self):
        text, gate = maintenance.gate_text({"tt", "materials"})
        self.assertIn("tt-posts", text)
        self.assertIn("drama-screenshot-material", text)
        self.assertNotIn("/api/drama-material", text)
        self.assertNotIn("x-posts", text)
        self.assertIn("503", gate)
        self.assertIn('~^[A-Z]+:/api/drama-screenshot-material/jobs/batch$', text)

    def test_no_main_api_oauth_or_metrics_trigger_stop(self):
        units = maintenance.TRIGGERS["tt"] + maintenance.TRIGGERS["x"]
        self.assertTrue(all(u.endswith((".timer", ".path")) for u in units))
        self.assertFalse(any("metric" in u for u in units))

    def test_source_scope_and_idle_guard(self):
        units = sum((value for key, value in fence.GROUPS.items()
                     if key != "materials"), [])
        self.assertEqual(len(units), len(set(units)))
        self.assertEqual(len([u for u in units if "tunnel" not in u]), 12)
        self.assertFalse(any("kronos" in u or "fb-page" in u for u in units))
        self.assertEqual(fence.GROUPS["drama"], ["drama-material-api.service"])
        self.assertNotIn("drama-material-api.service", fence.GROUPS["materials"])
        self.assertIn("gpu-worker-reverse-tunnel.service", fence.GROUPS["materials"])
        self.assertEqual(fence.GROUPS["materials-images"], fence.MATERIAL_IMAGE_UNITS)
        self.assertEqual(fence.GROUPS["materials-ad"], fence.MATERIAL_AD_UNITS)
        self.assertEqual(fence.GROUPS["materials"],
                         fence.MATERIAL_AD_UNITS[:2] + fence.MATERIAL_IMAGE_UNITS)
        idle = {"unit": "tt-gpu-publisher.service", "pid": 123, "threads": 1, "children": []}
        fence.assert_idle([idle])
        with self.assertRaises(RuntimeError):
            fence.assert_idle([dict(idle, threads=2)])
        with self.assertRaises(RuntimeError):
            fence.assert_idle([dict(idle, children=["234"])])

    def test_legacy_materials_apply_is_blocked_before_source_inspection(self):
        with mock.patch.object(sys, "argv", ["source_fence.py", "materials", "--apply"]), \
             mock.patch.object(fence.socket, "gethostname", return_value="VM-0-13-centos"), \
             mock.patch.object(fence, "source_storage_guard") as storage, \
             mock.patch.object(fence, "inspect") as inspect:
            with self.assertRaisesRegex(RuntimeError, "legacy coupled materials"):
                fence.main()
        storage.assert_not_called()
        inspect.assert_not_called()

    def test_materials_images_checkpoint_preserves_ad_only_lane(self):
        baseline = {"services": [{"unit": "ad-material-generation.service"}],
                    "tunnel": {"unit": fence.AD_ONLY_TUNNEL}}
        states = [
            {"unit": unit, "pid": 0, "control_pid": 0, "active": "inactive"}
            for unit in fence.MATERIAL_IMAGE_UNITS
        ]
        proof = {
            "coordinator_host": "VM-0-108-centos", "ready": True,
            "ad_requests_drained": True, "split_mode": "us-ad-only",
            "legacy_shared_tunnel_stopped": True,
            "legacy_burst_tunnel_stopped": True,
            "cpu_image_ports_owned_by_local_units": True,
            "cpu_ad_ports_owned_by_us_ad_only_tunnel": True,
            "ad_services_healthy": True, "us_ad_baseline": baseline,
        }
        fence.validate_materials_split_checkpoint(
            proof, "materials-images", states, ad_baseline=baseline)
        changed = dict(proof, cpu_ad_ports_owned_by_us_ad_only_tunnel=False)
        with self.assertRaises(RuntimeError):
            fence.validate_materials_split_checkpoint(
                changed, "materials-images", states, ad_baseline=baseline)
        live_tunnel = [dict(row) for row in states]
        next(row for row in live_tunnel
             if row["unit"] == "gpu-worker-reverse-tunnel.service")["pid"] = 12
        with self.assertRaisesRegex(RuntimeError, "legacy material tunnels"):
            fence.validate_materials_split_checkpoint(
                proof, "materials-images", live_tunnel, ad_baseline=baseline)

    def test_ad_only_tunnel_template_has_exact_two_strict_forwards(self):
        unit = (pathlib.Path(__file__).parent / "units" /
                "gpu-ad-only-reverse-tunnel.service.in").read_text()
        self.assertIn("StrictHostKeyChecking=yes", unit)
        self.assertIn("UserKnownHostsFile=/etc/gpu-ad-only-tunnel/known_hosts", unit)
        self.assertIn("-R 127.0.0.1:18796:127.0.0.1:8796", unit)
        self.assertIn("-R 127.0.0.1:18797:127.0.0.1:8797", unit)
        self.assertEqual(unit.count(" -R "), 2)
        for port in (18787, 18790, 18792, 18793, 18794, 18795, 18798):
            self.assertNotIn(":" + str(port) + ":", unit)

    def test_tunnel_permissions_remain_host_and_loopback_restricted(self):
        key = "dGVzdC1wdWJsaWMta2V5"
        fp = permissions.fingerprint(key)
        line = ('command="/usr/bin/sleep infinity",from="43.154.250.89",restrict,port-forwarding,'
                'permitlisten="127.0.0.1:18820",permitlisten="127.0.0.1:18788",'
                'permitlisten="127.0.0.1:18836" ssh-ed25519 ' + key + ' fixture\n')
        original = "ssh-rsa dW5yZWxhdGVk unrelated\n" + line
        updated = permissions.rewrite(original, expected_fingerprint=fp)
        self.assertEqual(updated.splitlines()[0], original.splitlines()[0])
        self.assertIn('from="43.154.250.89",restrict,port-forwarding', updated)
        self.assertEqual(updated.count("permitlisten="), 7)
        self.assertEqual(permissions.rewrite(updated, expected_fingerprint=fp), updated)
        self.assertEqual(permissions.rewrite(updated, rollback=True, expected_fingerprint=fp), original)
        with self.assertRaises(RuntimeError):
            permissions.rewrite(original.replace("restrict,", ""), expected_fingerprint=fp)

    def test_temporary_receiver_refuses_shell_and_arbitrary_paths(self):
        sha = "a" * 64
        self.assertEqual(receiver.parse_request("receive tt-state-final.tgz 128 " + sha),
                         ("tt-state-final.tgz", 128, sha))
        for request in ["sh", "receive ../../etc/passwd 128 " + sha,
                        "receive a.tgz 0 " + sha, "receive a.tgz 128 nope",
                        "receive a.tgz 128 " + sha + "; id",
                        "receive a.tgz 999999999999999 " + sha]:
            with self.assertRaises(ValueError):
                receiver.parse_request(request)

    def receiver_key(self, ending=b"\n"):
        blob = base64.b64encode(b"synthetic temporary receiver public key")
        options = (b'command="' + revoke.RECEIVE_COMMAND.encode("ascii")
                   + b'",from="43.166.178.132",restrict')
        line = options + b" ssh-ed25519 " + blob + b" " + revoke.COMMENT + ending
        return line, revoke.fingerprint(blob)

    def test_receiver_revocation_preserves_every_other_byte_and_newline(self):
        temporary, fp = self.receiver_key(ending=b"\r\n")
        # Includes an unrelated long-lived tunnel, CRLF, blank lines, a quoted
        # command and arbitrary comment bytes with no final newline.
        prefix = (b"# preserved comment\r\n\r\n"
                  b'command="/usr/bin/sleep infinity",from="43.154.250.89",restrict,'
                  b'port-forwarding ssh-ed25519 dW5yZWxhdGVk long-lived-tunnel\n')
        suffix = b"ssh-rsa c2Vjb25k unrelated owner's caf\xe9"
        self.assertEqual(revoke.rewrite(prefix + temporary + suffix, fp), prefix + suffix)
        self.assertEqual(revoke.rewrite(prefix + temporary.rstrip(b"\r\n"), fp), prefix)

    def test_receiver_revocation_requires_one_pinned_key(self):
        temporary, fp = self.receiver_key()
        for case, content, expected in (
            ("missing", b"# no receiver\n", fp),
            ("duplicate", temporary + temporary, fp),
            ("duplicate_wrong_scope", temporary + temporary.replace(b",restrict", b""), fp),
            ("wrong_fingerprint", temporary, "SHA256:wrong"),
        ):
            with self.subTest(case=case):
                with self.assertRaises(RuntimeError):
                    revoke.rewrite(content, expected)

    def test_receiver_revocation_rejects_changed_scope(self):
        temporary, fp = self.receiver_key()
        for old, new in (
            (b"43.166.178.132", b"43.166.187.96"),
            (b",restrict", b""),
            (b",restrict", b",restrict,port-forwarding"),
            (b",restrict", b",restrict,restrict"),
            (b"/usr/bin/python3.9", b"/usr/bin/python3"),
            (b"/control-code/", b"/../control-code/"),
            (b"7c54dedd9d6f59a9c46431aac7f1782f00ba71d1", b"other-commit"),
            (b"receive_archive.py", b"receive_archive.py; id"),
            (b"ssh-ed25519", b"ssh-rsa"),
            (revoke.COMMENT, b"long-lived-tunnel"),
        ):
            with self.subTest(scope=old.decode("ascii")):
                with self.assertRaises(RuntimeError):
                    revoke.rewrite(temporary.replace(old, new), fp)

    def test_receiver_revocation_defaults_to_dry_run_without_key_output(self):
        before, _ = self.receiver_key()
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["revoke_receiver.py"]), \
             mock.patch.object(revoke, "host_guard"), \
             mock.patch.object(revoke, "read_keys", return_value=(before, ())), \
             mock.patch.object(revoke, "rewrite", return_value=b""), \
             mock.patch.object(revoke, "apply_change") as apply, contextlib.redirect_stdout(output):
            revoke.main()
        apply.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {
            "dry_run": True, "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(b"").hexdigest(),
            "fingerprint": revoke.KEY_FP, "removed_count": 1,
        })

    def test_receiver_revocation_accepts_approved_root_filesystem_layout(self):
        row = "/ " + revoke.UUID + " rw,relatime"
        revoke.validate_data_mount(row, False, 42, 42)
        with self.assertRaises(RuntimeError):
            revoke.validate_data_mount(row, False, 42, 43)

    def test_receiver_revocation_accepts_approved_dedicated_mount_layout(self):
        row = "/data " + revoke.UUID + " rw,noatime"
        revoke.validate_data_mount(row, True, 42, 43)
        for changed in (
            "/data wrong-uuid rw,noatime",
            "/data " + revoke.UUID + " ro,noatime",
            "/other " + revoke.UUID + " rw,noatime",
        ):
            with self.subTest(row=changed), self.assertRaises(RuntimeError):
                revoke.validate_data_mount(changed, True, 42, 43)
        with self.assertRaises(RuntimeError):
            revoke.validate_data_mount(row, False, 42, 43)

    def test_unit_retirement_copies_across_disks_without_overwriting_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            local = root / "worker.service"
            backup = root / "data-backup"
            backup.mkdir(mode=0o700)
            local.write_bytes(b"[Service]\nExecStart=/worker\n")
            original = local.read_bytes()
            fence.retire_local_unit(local, backup)
            self.assertFalse(local.exists())
            self.assertEqual((backup / "retired-local.service").read_bytes(), original)
            local.write_bytes(b"changed by another deploy")
            with self.assertRaises(RuntimeError):
                fence.retire_local_unit(local, backup)
            self.assertTrue(local.exists())

    def test_drama_pair_update_preserves_unrelated_configuration(self):
        old = ('OTHER=keep-me\n# keep comment\nGPU_VIDEO_WORKER_URL=http://127.0.0.1:18787\n'
               'GPU_VIDEO_WORKER_TOKEN=fixture-old\n')
        new = {"GPU_VIDEO_WORKER_URL": "http://127.0.0.1:18788", "GPU_VIDEO_WORKER_TOKEN": "fixture-new#value"}
        updated = finalize.replace_pair(old, new)
        self.assertTrue(updated.startswith("OTHER=keep-me\n# keep comment\n"))
        self.assertEqual(finalize.read_values(updated), new)
        with self.assertRaises(RuntimeError):
            finalize.replace_pair(old + "GPU_VIDEO_WORKER_TOKEN=duplicate\n", new)

    def test_drama_drain_configuration_compares_token_without_disclosure(self):
        secret = "fixture-token-must-never-be-emitted"
        with tempfile.TemporaryDirectory() as folder:
            paths = (pathlib.Path(folder) / "cpu.env", pathlib.Path(folder) / "persistent.env")
            for path in paths:
                path.write_text("GPU_VIDEO_WORKER_URL=http://127.0.0.1:18788\n"
                                "GPU_VIDEO_WORKER_TOKEN='" + secret + "'\n")
            state = {"active": "active", "substate": "running", "pid": 321,
                     "control_pid": 0, "control_group": drama.EXPECTED_CONTROL_GROUP,
                     "nrestarts": 0, "start_monotonic": "123"}
            running = {"GPU_VIDEO_WORKER_URL": b"http://127.0.0.1:18788",
                       "GPU_VIDEO_WORKER_TOKEN": secret.encode("utf-8")}
            with mock.patch.object(drama, "ENV_PATHS", paths), \
                 mock.patch.object(drama, "unit_state", return_value=state), \
                 mock.patch.object(drama, "process_start_ticks", return_value="987"), \
                 mock.patch.object(drama, "process_gpu_pair", return_value=running):
                result = drama.inspect_api_configuration()
        rendered = json.dumps(result)
        self.assertNotIn(secret, rendered)
        self.assertTrue(result["tokens_match_without_disclosure"])
        self.assertEqual(result["effective_url"], "http://127.0.0.1:18788")

    def test_drama_drain_health_uses_only_exact_healthz_get(self):
        calls = []

        class Response(object):
            status = 200

            def read(self, limit):
                self.limit = limit
                return b'{"ok":true,"role":"media-only"}'

        class Connection(object):
            def __init__(self, host, port, timeout):
                calls.append(("connect", host, port, timeout))

            def request(self, method, path, headers):
                calls.append(("request", method, path, headers))

            def getresponse(self):
                return Response()

            def close(self):
                calls.append(("close",))

        with mock.patch.object(drama.http.client, "HTTPConnection", Connection):
            result = drama.inspect_health()
        self.assertEqual(result["status"], 200)
        self.assertEqual(calls[1][1:3], ("GET", "/healthz"))
        self.assertNotIn("Authorization", calls[1][3])
        self.assertEqual(len([row for row in calls if row[0] == "request"]), 1)

    def test_drama_drain_requires_exact_paused_cron(self):
        before = "MAILTO=ops@example.test\n* * * * * /root/run_auto_cover_synthesis.sh\n"
        expected = ("MAILTO=ops@example.test\n" + drama.CRON_MARKER +
                    "* * * * * /root/run_auto_cover_synthesis.sh\n")
        self.assertEqual(drama.paused_crontab(before, expected), expected)
        with self.assertRaises(RuntimeError):
            drama.paused_crontab(before, before)
        with self.assertRaises(RuntimeError):
            drama.paused_crontab(before + "0 0 * * * /root/run_auto_cover_synthesis.sh\n", expected)

    def test_drama_drain_requires_final_paused_journal_phase(self):
        before = "* * * * * /root/run_auto_cover_synthesis.sh\n"
        current = drama.CRON_MARKER + before
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder)
            pause_path = base / "materials-triggers.json"
            snapshot = base / "materials-crontab-before.txt"
            snapshot.write_text(before)
            state = {"version": 2, "run_id": drama.RUN_ID, "group": "materials",
                     "phase": "paused", "revision": 3, "restored": False,
                     "original": {unit: "inactive" for unit in drama.TRIGGERS["materials"]}}
            pause_path.write_text(json.dumps(state))
            unit = {"active": "inactive", "substate": "dead", "pid": 0,
                    "control_pid": 0, "control_group": "", "nrestarts": 0,
                    "start_monotonic": "1"}
            with mock.patch.object(drama, "BASE", base), \
                 mock.patch.object(drama, "unit_state", return_value=unit), \
                 mock.patch.object(drama, "run",
                                   return_value=mock.Mock(stdout=current, returncode=0)):
                result = drama.inspect_pause()
                self.assertEqual(result["journal_phase"], "paused")
                self.assertEqual(result["journal_revision"], 3)
                state["phase"] = "resume_incomplete"
                pause_path.write_text(json.dumps(state))
                with self.assertRaisesRegex(RuntimeError, "restored or is ambiguous"):
                    drama.inspect_pause()

    def test_drama_drain_sqlite_requires_terminal_jobs_and_leases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "jobs.sqlite3"
            connection = sqlite3.connect(str(path))
            connection.execute("CREATE TABLE drama_material_job (job_id TEXT,status TEXT)")
            connection.execute("CREATE TABLE drama_material_job_worker_lease (job_id TEXT,status TEXT)")
            connection.execute("INSERT INTO drama_material_job VALUES ('done-job','done')")
            connection.execute("INSERT INTO drama_material_job_worker_lease VALUES ('done-job','done')")
            connection.commit()
            connection.close()
            result = drama.inspect_database(path)
            self.assertEqual(result["active_jobs"], 0)
            self.assertTrue(result["no_unknown"])
            connection = sqlite3.connect(str(path))
            connection.execute("INSERT INTO drama_material_job VALUES ('live-job','rendering')")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RuntimeError, "jobs have not drained"):
                drama.inspect_database(path)

    def test_drama_drain_rejects_unknown_semantics(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "jobs.sqlite3"
            connection = sqlite3.connect(str(path))
            connection.execute("CREATE TABLE drama_material_job (job_id TEXT,status TEXT,unknown_outcome INTEGER)")
            connection.execute("CREATE TABLE drama_material_job_worker_lease (job_id TEXT,status TEXT)")
            connection.execute("INSERT INTO drama_material_job VALUES ('done-job','done',0)")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(RuntimeError, "unknown-outcome semantics"):
                drama.inspect_database(path)

    def test_drama_drain_checkpoint_matches_source_fence_contract(self):
        verification = {"materials_gate": {"materials_active": True},
                        "materials_pause": {"record_restored": False},
                        "cpu_api": {"pid": 321}, "hk_health": {"status": 200},
                        "database": {"active_jobs": 0, "active_leases": 0, "no_unknown": True},
                        "drain_samples": {"sample_count": 3, "stable": True},
                        "process_scope": {"cgroup_pids": [321], "descendant_pids": []}}
        with mock.patch.object(drama, "storage_guard"), \
             mock.patch.object(drama.socket, "gethostname", return_value=drama.CPU_HOST), \
             mock.patch.object(drama, "inspection_pass", side_effect=[verification, verification]):
            result = drama.inspect()
        self.assertEqual(result["group"], "drama")
        self.assertEqual(result["coordinator_host"], drama.CPU_HOST)
        for field in ("ready", "new_admission_closed", "triggers_paused", "cpu_drained", "no_unknown"):
            self.assertIs(result[field], True)
        self.assertEqual(result["business_requests_sent"], 0)
        self.assertEqual(result["health_get_requests_completed"], 2)
        self.assertTrue(result["stability"]["identical"])

    def test_drama_drain_rejects_state_change_between_final_passes(self):
        first = {"materials_gate": {"map_sha256": "a"}, "cpu_api": {"pid": 321}}
        second = {"materials_gate": {"map_sha256": "b"}, "cpu_api": {"pid": 321}}
        with mock.patch.object(drama, "storage_guard"), \
             mock.patch.object(drama.socket, "gethostname", return_value=drama.CPU_HOST), \
             mock.patch.object(drama, "inspection_pass", side_effect=[first, second]):
            with self.assertRaisesRegex(RuntimeError, "changed between verification passes"):
                drama.inspect()

    def test_drama_drain_rejects_18788_business_connection(self):
        database = {"job_status_counts": {"done": 1}, "lease_status_counts": {"done": 1},
                    "active_jobs": 0, "active_leases": 0, "no_unknown": True}
        connections = {"legacy_18787_connected": 0, "legacy_18787_established": 0,
                       "hk_18788_established_after_health": 1}
        with mock.patch.object(drama, "inspect_database", return_value=database), \
             mock.patch.object(drama, "endpoint_connection_counts", return_value=connections):
            with self.assertRaisesRegex(RuntimeError, "business HTTP connection"):
                drama.inspect_drained_samples(samples=1, interval=0)

    def test_drama_drain_uses_only_api_systemd_cgroup_and_rejects_children(self):
        api = {"pid": 321, "control_group": drama.EXPECTED_CONTROL_GROUP}
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            cgroup = root / "system.slice" / "drama-material-api.service"
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.procs").write_text("321\n")
            with mock.patch.object(drama, "SYSTEMD_CGROUP_ROOT", root), \
                 mock.patch.object(drama, "process_descendants", return_value=set()):
                result = drama.inspect_api_process_scope(api)
            self.assertEqual(result["cgroup_pids"], [321])
            self.assertFalse(result["host_wide_process_scan_performed"])
            (cgroup / "cgroup.procs").write_text("321\n654\n")
            with mock.patch.object(drama, "SYSTEMD_CGROUP_ROOT", root), \
                 mock.patch.object(drama, "process_descendants", return_value={654}), \
                 mock.patch.object(drama, "process_category", return_value="ffmpeg"):
                with self.assertRaisesRegex(RuntimeError, "drama-related children"):
                    drama.inspect_api_process_scope(api)

    def test_drama_source_fence_validates_full_cpu_checkpoint_contract(self):
        proof = {
            "ready": True, "coordinator_host": "VM-0-108-centos",
            "business_requests_sent": 0, "legacy_18787_connections": 0,
            "legacy_18787_established_connections": 0,
            "hk_18788_business_http_connections": 0, "health_get_requests_completed": 2,
            "hk_health": {"url": "http://127.0.0.1:18788/healthz", "method": "GET",
                          "status": 200, "body": {"ok": True, "role": "media-only"}},
            "cpu_api": {"effective_url": "http://127.0.0.1:18788",
                        "active": "active", "substate": "running",
                        "control_pid": 0,
                        "control_group": "/system.slice/drama-material-api.service",
                        "both_files_point_to_expected_url": True,
                        "tokens_match_without_disclosure": True,
                        "running_environment_matches": True, "pid": 321,
                        "configuration_files": [
                            {"path": "/etc/drama-synthesis/cpu.env"},
                            {"path": "/root/drama_material_service/.env"},
                        ]},
            "materials_gate": {"materials_active": True, "groups": ["materials"]},
            "materials_pause": {
                "record_restored": False, "cron_paused": True,
                "journal_version": 2, "journal_run_id": fence.RUN_ID,
                "journal_group": "materials", "journal_phase": "paused",
                "journal_revision": 3,
                "test_services": {
                    "ad-material-frontend-test.service": {"active": "inactive", "substate": "dead", "pid": 0},
                    "drama-material-api-test.service": {"active": "inactive", "substate": "dead", "pid": 0},
                },
            },
            "database": {"active_jobs": 0, "active_leases": 0, "no_unknown": True,
                         "unknown_semantics": "not_applicable_and_absent"},
            "drain_samples": {"sample_count": 3, "stable": True},
            "process_scope": {
                "cgroup_version": 1, "controller": "systemd",
                "control_group": "/system.slice/drama-material-api.service",
                "main_pid": 321, "cgroup_pids": [321], "descendant_pids": [],
                "drama_related_child_categories": {"ffmpeg": 0, "ffprobe": 0,
                                                    "codex": 0, "other": 0},
                "host_wide_process_scan_performed": False,
            },
            "stability": {"verification_passes": 2, "identical": True},
        }
        critical_fields = ("materials_gate", "materials_pause", "cpu_api", "hk_health",
                           "database", "drain_samples", "process_scope")
        critical = {name: proof[name] for name in critical_fields}
        proof["stability"]["critical_snapshot_sha256"] = hashlib.sha256(json.dumps(
            critical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        fence.validate_drama_checkpoint(proof)
        with self.assertRaises(RuntimeError):
            fence.validate_drama_checkpoint(dict(proof, business_requests_sent=1))
        changed = dict(proof)
        changed["database"] = dict(proof["database"], active_leases=1)
        with self.assertRaises(RuntimeError):
            fence.validate_drama_checkpoint(changed)
        self_consistent_fields_changed = dict(proof)
        self_consistent_fields_changed["database"] = dict(proof["database"], ignored_extra="tamper")
        with self.assertRaisesRegex(RuntimeError, "double-verification"):
            fence.validate_drama_checkpoint(self_consistent_fields_changed)

    def test_drama_source_fence_requires_single_thread_and_preserves_tunnel_identity(self):
        source = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                  "substate": "running", "threads": 1, "children": []}
        shared = complete_shared_tunnel()
        with mock.patch.object(fence, "port_rows", return_value=[]):
            fence.validate_drama_preflight([source])
        fence.validate_shared_tunnel_baseline(shared)
        with mock.patch.object(fence, "port_rows", return_value=[]), \
             mock.patch.object(fence, "inspect", return_value={
                 "unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive", "substate": "dead",
                 "enabled": "masked", "load_state": "masked",
                 "control_pid": 0, "control_group": "", "cgroup_pids": []}), \
             mock.patch.object(fence, "is_persistent_mask", return_value=True), \
             mock.patch.object(fence, "shared_tunnel_snapshot", return_value=shared):
            _, after = fence.validate_drama_fenced(shared)
        self.assertEqual(after, shared)
        with mock.patch.object(fence, "port_rows", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "single-threaded"):
                fence.validate_drama_preflight([dict(source, threads=2)])

    def test_drama_source_fence_detects_shared_tunnel_restart(self):
        before = complete_shared_tunnel()
        after = dict(before, pid=789, nrestarts=1)
        final = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive", "substate": "dead",
                 "enabled": "masked", "load_state": "masked",
                 "control_pid": 0, "control_group": "", "cgroup_pids": []}
        with mock.patch.object(fence, "port_rows", return_value=[]), \
             mock.patch.object(fence, "inspect", return_value=final), \
             mock.patch.object(fence, "is_persistent_mask", return_value=True), \
             mock.patch.object(fence, "shared_tunnel_snapshot", return_value=after):
            with self.assertRaisesRegex(RuntimeError, "shared drama tunnel changed"):
                fence.validate_drama_fenced(before)

    def test_drama_source_fence_rejects_end_state_change(self):
        shared = {"pid": 456, "nrestarts": 0, "unit_sha256": "a" * 64}
        unsafe = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                  "substate": "running", "enabled": "masked", "control_pid": 0,
                  "control_group": "/system.slice/drama-material-api.service"}
        with mock.patch.object(fence, "inspect", return_value=unsafe), \
             mock.patch.object(fence, "port_rows", return_value=[]), \
             mock.patch.object(fence, "shared_tunnel_snapshot", return_value=shared):
            with self.assertRaisesRegex(RuntimeError, "final fence verification failed"):
                fence.validate_drama_fenced(shared)

    def test_drama_unit_definition_preserves_all_paths_and_symlink_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            fragment = root / "drama.service"
            target = root / "real-dropin.conf"
            fragment.write_text("[Service]\nExecStart=/worker\n")
            target.write_text("[Service]\nEnvironmentFile=/private/env\n")

            def prop(unit, name):
                if name == "FragmentPath":
                    return str(fragment)
                if name == "DropInPaths":
                    return str(target)
                raise AssertionError(name)

            with mock.patch.object(fence, "prop", side_effect=prop):
                definition = fence.unit_definition_snapshot(fence.DRAMA_UNIT)
            self.assertEqual(definition["fragment"]["kind"], "file")
            self.assertEqual(definition["dropins"][0]["kind"], "file")
            backup = root / "backup"
            backup.mkdir(mode=0o700)
            state = {"unit": fence.DRAMA_UNIT, "definition": definition,
                     "fragment": str(fragment)}
            with mock.patch.object(fence, "unit_definition_snapshot", return_value=definition):
                fence.backup_unit_definition(state, backup)
            self.assertEqual((backup / "original.service").read_bytes(), fragment.read_bytes())
            self.assertEqual((backup / "dropins/000.service").read_bytes(), target.read_bytes())
            self.assertEqual(json.loads((backup / "definition.json").read_text()), definition)
            capability = fence.verified_definition_backup(backup)
            self.assertTrue(capability["verified"])
            self.assertEqual(capability["definition_mode"], "archived")
            self.assertTrue(capability["restorable"])
            fake_link = root / "linked-dropin.conf"
            with mock.patch.object(fence, "path_lexists", return_value=True), \
                 mock.patch.object(fence, "path_is_symlink", return_value=True), \
                 mock.patch.object(fence, "resolve_definition_path", return_value=target), \
                 mock.patch.object(fence.os, "readlink", return_value="../real-dropin.conf"):
                link_record = fence.definition_path_record(str(fake_link))
            self.assertEqual(link_record["kind"], "symlink")
            self.assertEqual(link_record["link_target"], "../real-dropin.conf")
            self.assertEqual(link_record["content_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())

    def test_drama_unit_definition_backup_rejects_symlink_member(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            payload = b"[Service]\n"
            definition = {"fragment": {"path": "/etc/systemd/system/drama-material-api.service",
                                          "kind": "file",
                                          "content_sha256": hashlib.sha256(payload).hexdigest()},
                          "dropins": []}
            definition["definition_sha256"] = hashlib.sha256(json.dumps(
                definition, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            (root / "definition.json").write_text(json.dumps(definition))
            target = root / "actual.service"
            target.write_bytes(payload)
            try:
                (root / "original.service").symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("test platform cannot create a symlink")
            capability = fence.verified_definition_backup(root)
            self.assertFalse(capability["verified"])
            self.assertEqual(capability["error_type"], "RuntimeError")

    def _absent_drama_topology(self, root):
        systemd_root = root / "etc-systemd"
        run_root = root / "run-systemd"
        wants_dir = systemd_root / "multi-user.target.wants"
        wants_dir.mkdir(parents=True)
        run_root.mkdir(parents=True)
        fragment = systemd_root / fence.DRAMA_UNIT
        dropin = systemd_root / (fence.DRAMA_UNIT + ".d")
        wants = wants_dir / fence.DRAMA_UNIT
        try:
            wants.symlink_to(str(fragment))
        except (OSError, NotImplementedError):
            self.skipTest("test platform cannot create a symlink")
        state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                 "substate": "running", "control_pid": 0,
                 "control_group": "/system.slice/" + fence.DRAMA_UNIT,
                 "cgroup_pids": [123],
                 "enabled": "enabled", "fragment": str(fragment), "threads": 1,
                 "children": [], "pid_start_ticks": 8001, "nrestarts": 0,
                 "start_monotonic": "100", "active_enter_monotonic": "101",
                 "load_state": "loaded", "id": fence.DRAMA_UNIT,
                 "names": [fence.DRAMA_UNIT]}
        values = {"FragmentPath": str(fragment), "DropInPaths": "", "LoadState": "loaded",
                  "Id": fence.DRAMA_UNIT, "Names": fence.DRAMA_UNIT,
                  "ActiveState": "active", "SubState": "running",
                  "UnitFileState": "enabled", "MainPID": "123", "ControlPID": "0",
                  "ControlGroup": "/system.slice/" + fence.DRAMA_UNIT,
                  "ExecMainStartTimestampMonotonic": "100",
                  "ActiveEnterTimestampMonotonic": "101", "NRestarts": "0"}
        return {"fragment": fragment, "dropin": dropin, "wants": wants,
                "systemd_root": systemd_root, "run_root": run_root,
                "state": state, "values": values}

    def test_drama_absent_fragment_is_explicit_nonrestorable_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            fixture = self._absent_drama_topology(root)
            unit_paths = unit_path_snapshot_for_roots(
                fixture["systemd_root"], fixture["run_root"])
            with mock.patch.object(fence, "DRAMA_LOCAL_FRAGMENT", fixture["fragment"]), \
                 mock.patch.object(fence, "DRAMA_DROPIN_DIR", fixture["dropin"]), \
                 mock.patch.object(fence, "DRAMA_WANTS_LINK", fixture["wants"]), \
                 mock.patch.object(fence, "systemd_unit_path_snapshot",
                                   return_value=unit_paths), \
                 mock.patch.object(fence, "prop",
                                   side_effect=lambda unit, name: fixture["values"][name]), \
                 mock.patch.object(fence, "proc_start_ticks", return_value=8001), \
                 mock.patch.object(fence, "systemd_cgroup_pids", return_value=[123]), \
                 mock.patch.object(fence, "inspect", return_value=fixture["state"]):
                definition = fence.unit_definition_snapshot(fence.DRAMA_UNIT,
                                                            state=fixture["state"])
                self.assertEqual(definition["definition_mode"], "loaded_fragment_absent")
                self.assertFalse(definition["fragment"]["restorable"])
                self.assertFalse(definition["fragment"]["can_retire_local"])
                self.assertEqual(definition["fragment"]["loaded_unit"]["pid_start_ticks"], 8001)
                backup = root / "backup"
                backup.mkdir(mode=0o700)
                state = dict(fixture["state"], definition=definition)
                fence.backup_unit_definition(state, backup)
                self.assertFalse(fence.path_lexists(backup / "original.service"))
                capability = fence.verified_definition_backup(backup)
                self.assertTrue(capability["verified"])
                self.assertEqual(capability["definition_mode"], "loaded_fragment_absent")
                self.assertFalse(capability["restorable"])
                self.assertFalse(capability["can_retire_local"])

                (backup / "original.service").write_text("invented content")
                self.assertFalse(fence.verified_definition_backup(backup)["verified"])

    def test_drama_absent_fragment_rejects_nonabsolute_and_missing_dropin(self):
        with self.assertRaisesRegex(RuntimeError, "non-absolute"):
            fence.definition_path_record("relative.service")
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            missing = root / "missing.conf"
            with self.assertRaisesRegex(RuntimeError, "missing"):
                fence.definition_path_record(str(missing))
            fixture = self._absent_drama_topology(root)
            fixture["values"]["DropInPaths"] = str(missing)
            with mock.patch.object(fence, "DRAMA_LOCAL_FRAGMENT", fixture["fragment"]), \
                 mock.patch.object(fence, "DRAMA_DROPIN_DIR", fixture["dropin"]), \
                 mock.patch.object(fence, "prop",
                                   side_effect=lambda unit, name: fixture["values"][name]):
                with self.assertRaisesRegex(RuntimeError, "unsupported absent"):
                    fence.unit_definition_snapshot(fence.DRAMA_UNIT, state=fixture["state"])
                with self.assertRaisesRegex(RuntimeError, "unsupported absent"):
                    fence.unit_definition_snapshot(fence.DRAMA_SHARED_TUNNEL,
                                                   state=fixture["state"])

    def test_drama_absent_fragment_detects_file_and_symlink_appearance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            fixture = self._absent_drama_topology(root)
            unit_paths = unit_path_snapshot_for_roots(
                fixture["systemd_root"], fixture["run_root"])
            with mock.patch.object(fence, "DRAMA_LOCAL_FRAGMENT", fixture["fragment"]), \
                 mock.patch.object(fence, "DRAMA_DROPIN_DIR", fixture["dropin"]), \
                 mock.patch.object(fence, "DRAMA_WANTS_LINK", fixture["wants"]), \
                 mock.patch.object(fence, "systemd_unit_path_snapshot",
                                   return_value=unit_paths), \
                 mock.patch.object(fence, "prop",
                                   side_effect=lambda unit, name: fixture["values"][name]), \
                 mock.patch.object(fence, "proc_start_ticks", return_value=8001), \
                 mock.patch.object(fence, "systemd_cgroup_pids", return_value=[123]):
                definition = fence.unit_definition_snapshot(fence.DRAMA_UNIT,
                                                            state=fixture["state"])
                for kind in ("file", "symlink"):
                    target = root / ("target-" + kind)
                    target.write_text("[Service]\n")
                    if kind == "file":
                        fixture["fragment"].write_text("[Service]\n")
                    else:
                        fixture["fragment"].symlink_to(target)
                    with self.assertRaisesRegex(RuntimeError, "fragment or drop-in appeared"):
                        fence.validate_recorded_definition_current(definition, fixture["state"])
                    fixture["fragment"].unlink()

    def test_loaded_absent_schema_binds_manager_process_and_cgroup_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            fragment = root / fence.DRAMA_UNIT
            dropin = root / (fence.DRAMA_UNIT + ".d")
            wants = root / "multi-user.target.wants" / fence.DRAMA_UNIT
            state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                     "substate": "running", "enabled": "enabled", "control_pid": 0,
                     "control_group": "/system.slice/" + fence.DRAMA_UNIT,
                     "fragment": str(fragment), "pid_start_ticks": 8001,
                     "cgroup_pids": [123], "threads": 1, "children": [], "nrestarts": 0,
                     "start_monotonic": "100", "active_enter_monotonic": "101",
                     "load_state": "loaded", "id": fence.DRAMA_UNIT,
                     "names": [fence.DRAMA_UNIT]}
            values = {"LoadState": "loaded", "Id": fence.DRAMA_UNIT,
                      "Names": fence.DRAMA_UNIT, "FragmentPath": str(fragment),
                      "DropInPaths": "", "ActiveState": "active", "SubState": "running",
                      "UnitFileState": "enabled", "MainPID": "123", "ControlPID": "0",
                      "ControlGroup": "/system.slice/" + fence.DRAMA_UNIT,
                      "ExecMainStartTimestampMonotonic": "100",
                      "ActiveEnterTimestampMonotonic": "101", "NRestarts": "0"}
            wants_record = {"path": str(wants), "kind": "dangling-symlink"}
            unit_paths = unit_path_snapshot_for_roots(root)
            with mock.patch.object(fence, "DRAMA_LOCAL_FRAGMENT", fragment), \
                 mock.patch.object(fence, "DRAMA_DROPIN_DIR", dropin), \
                 mock.patch.object(fence, "prop", side_effect=lambda unit, name: values[name]), \
                 mock.patch.object(fence, "proc_start_ticks", return_value=8001), \
                 mock.patch.object(fence, "systemd_cgroup_pids", return_value=[123]), \
                 mock.patch.object(fence, "systemd_unit_path_snapshot",
                                   return_value=unit_paths), \
                 mock.patch.object(fence, "path_lexists", return_value=False), \
                 mock.patch.object(fence, "drama_wants_link_record", return_value=wants_record):
                record = fence.loaded_absent_fragment_record(
                    fence.DRAMA_UNIT, str(fragment), [], state)
                self.assertEqual(record["loaded_unit"]["cgroup_pids"], [123])
                for field, changed in (("pid_start_ticks", 8002), ("cgroup_pids", []),
                                       ("nrestarts", 1), ("control_group", "/changed")):
                    with self.assertRaises(RuntimeError):
                        fence.loaded_absent_fragment_record(
                            fence.DRAMA_UNIT, str(fragment), [], dict(state, **{field: changed}))
                values["Names"] = fence.DRAMA_UNIT + " unexpected-alias.service"
                with self.assertRaisesRegex(RuntimeError, "exact identity"):
                    fence.loaded_absent_fragment_record(
                        fence.DRAMA_UNIT, str(fragment), [], state)

    def test_loaded_absent_capability_rejects_invented_archive_and_schema_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            backup = pathlib.Path(folder)
            definition = loaded_absent_definition()
            fence.write_private_json(backup / "definition.json", definition)
            capability = fence.verified_definition_backup(backup)
            self.assertEqual(capability, absent_capability())
            (backup / "original.service").write_text("invented")
            self.assertFalse(fence.verified_definition_backup(backup)["verified"])
            (backup / "original.service").unlink()
            tampered = json.loads(json.dumps(definition))
            tampered["fragment"]["can_retire_local"] = True
            core = {name: tampered[name] for name in
                    ("fragment", "dropins", "schema_version", "definition_mode", "unit",
                     "restorable")}
            tampered["definition_sha256"] = hashlib.sha256(json.dumps(
                core, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            (backup / "definition.json").unlink()
            fence.write_private_json(backup / "definition.json", tampered)
            self.assertFalse(fence.verified_definition_backup(backup)["verified"])

    def test_loaded_absent_failure_never_retires_or_masks_reappeared_fragment(self):
        original = RuntimeError("post-stop drift")
        calls = []

        def command(args):
            calls.append(args)
            return {"rc": 0, "error_type": None}

        with mock.patch.object(fence, "best_effort_command", side_effect=command), \
             mock.patch.object(fence, "verified_definition_backup", return_value=absent_capability()), \
             mock.patch.object(fence, "path_lexists",
                               side_effect=lambda path: path == fence.DRAMA_LOCAL_FRAGMENT), \
             mock.patch.object(fence, "is_persistent_mask", return_value=False), \
             mock.patch.object(fence, "closure_state",
                               return_value=({"active": "inactive"}, False, None)), \
             mock.patch.object(fence, "shared_tunnel_snapshot", return_value={"pid": 456}), \
             mock.patch.object(fence, "write_failure_evidence"), \
             mock.patch.object(fence, "retire_local_unit") as retire:
            with self.assertRaisesRegex(RuntimeError, "HIGH RISK"):
                fence.fail_closed_drama("retire-local-unit", {"pid": 456}, original,
                                        pathlib.Path("C:/evidence"))
        retire.assert_not_called()
        self.assertFalse(any(args[:2] == ["systemctl", "disable"] for args in calls))
        self.assertFalse(any(args[:2] == ["systemctl", "mask"] for args in calls))
        self.assertFalse(any(fence.DRAMA_SHARED_TUNNEL in str(args) for args in calls))

    def test_failure_closure_requires_shared_tunnel_identity_unchanged(self):
        with mock.patch.object(fence, "best_effort_command",
                               return_value={"rc": 0, "error_type": None}), \
             mock.patch.object(fence, "verified_definition_backup", return_value=archived_capability()), \
             mock.patch.object(fence, "closure_state",
                               return_value=({"active": "inactive"}, True, None)), \
             mock.patch.object(fence, "shared_tunnel_snapshot", return_value={"pid": 789}), \
             mock.patch.object(fence, "write_failure_evidence") as evidence:
            with self.assertRaisesRegex(RuntimeError, "HIGH RISK"):
                fence.fail_closed_drama("daemon-reload", {"pid": 456}, RuntimeError("failure"),
                                        pathlib.Path("C:/evidence"))
        self.assertFalse(evidence.call_args[0][4])

    def test_fail_closed_loaded_absent_requires_one_complete_final_topology_snapshot(self):
        definition = loaded_absent_definition()
        loaded = definition["fragment"]["loaded_unit"]
        original_state = {"pid": loaded["main_pid"],
                          "pid_start_ticks": loaded["pid_start_ticks"],
                          "control_group": loaded["control_group"]}
        shared = complete_shared_tunnel()
        guard_before = {"enablement_kind": "dangling-symlink", "dropin_absent": True,
                        "fragment_absent": True, "persistent_mask": False}
        guard_after = {"enablement_kind": "absent", "dropin_absent": True,
                       "fragment_absent": True, "persistent_mask": False}
        final_source = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive",
                        "substate": "dead", "enabled": "masked", "load_state": "masked",
                        "control_pid": 0, "control_group": "", "cgroup_pids": []}
        for drift in (None, "dropin", "wants", "alias", "old-process", "cgroup",
                      "cgroup-pids"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as folder:
                backup = pathlib.Path(folder)
                fence.write_private_json(backup / "definition.json", definition)
                source = dict(final_source)
                if drift == "cgroup":
                    source["control_group"] = loaded["control_group"]
                if drift == "cgroup-pids":
                    source["cgroup_pids"] = [loaded["main_pid"]]
                alias_rows = ([{"path": "/run/systemd/system/unexpected-alias.service"}]
                              if drift == "alias" else [])
                shared_probe = mock.Mock(return_value=shared)
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(
                        fence, "verified_definition_backup", return_value=absent_capability()))
                    stack.enter_context(mock.patch.object(
                        fence, "loaded_absent_mutation_guard",
                        side_effect=[guard_before, guard_after]))
                    stack.enter_context(mock.patch.object(
                        fence, "remove_loaded_absent_enablement",
                        return_value={"attempted": True, "rc": 0,
                                      "postcondition": "absent"}))
                    command = stack.enter_context(mock.patch.object(
                        fence, "best_effort_command",
                        return_value={"rc": 0, "error_type": None}))
                    stack.enter_context(mock.patch.object(
                        fence, "is_persistent_mask", return_value=True))
                    stack.enter_context(mock.patch.object(fence, "fsync_directory"))
                    stack.enter_context(mock.patch.object(
                        fence, "current_systemd_unit_paths",
                        return_value=definition["fragment"]["systemd_unit_paths"]))
                    stack.enter_context(mock.patch.object(fence, "inspect", return_value=source))
                    stack.enter_context(mock.patch.object(
                        fence, "path_lexists",
                        side_effect=lambda path: ((drift == "dropin" and
                                                   path == fence.DRAMA_DROPIN_DIR) or
                                                  (drift == "wants" and
                                                   path == fence.DRAMA_WANTS_LINK))))
                    stack.enter_context(mock.patch.object(
                        fence, "drama_fragment_symlink_records", return_value=alias_rows))
                    stack.enter_context(mock.patch.object(
                        fence, "original_process_identity_is_gone",
                        return_value=drift != "old-process"))
                    stack.enter_context(mock.patch.object(fence, "port_rows", return_value=[]))
                    stack.enter_context(mock.patch.object(
                        fence, "shared_tunnel_snapshot", shared_probe))
                    evidence = stack.enter_context(mock.patch.object(
                        fence, "write_failure_evidence"))
                    if drift is None:
                        fence.fail_closed_drama(
                            "daemon-reload", shared, RuntimeError("fixture"), backup,
                            original_state=original_state)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "HIGH RISK"):
                            fence.fail_closed_drama(
                                "daemon-reload", shared, RuntimeError("fixture"), backup,
                                original_state=original_state)
                self.assertEqual(shared_probe.call_count, 1)
                self.assertEqual(evidence.call_args[0][4], drift is None)
                self.assertFalse(any(
                    fence.DRAMA_SHARED_TUNNEL in str(call)
                    for call in command.call_args_list))

    def test_persistent_mask_must_be_direct_dev_null_link(self):
        local = pathlib.Path("C:/fixture/drama.service")
        with mock.patch.object(fence, "path_lexists", return_value=True), \
             mock.patch.object(fence, "path_is_symlink", return_value=True), \
             mock.patch.object(fence.os, "readlink", return_value="/dev/null"):
            self.assertTrue(fence.is_persistent_mask(local))
        for target in ("../mask-target", "/tmp/indirect-dev-null"):
            with mock.patch.object(fence, "path_lexists", return_value=True), \
                 mock.patch.object(fence, "path_is_symlink", return_value=True), \
                 mock.patch.object(fence.os, "readlink", return_value=target):
                self.assertFalse(fence.is_persistent_mask(local))

    def test_systemd_unit_path_snapshot_accepts_real_manager_subset_and_detects_drift(self):
        manager = " ".join([
            "/etc/systemd/system.control", "/run/systemd/system.control",
            "/run/systemd/transient", "/run/systemd/generator.early",
            "/etc/systemd/system", "/run/systemd/system",
            "/run/systemd/generator", "/usr/lib/systemd/system"])
        analyzed = "\n".join(fence.SYSTEMD_UNIT_PATH_ALLOWLIST)
        absent = lambda value: {"path": value, "kind": "absent"}
        outputs = [mock.Mock(stdout=value) for value in
                   (manager, analyzed, manager, analyzed)]
        with mock.patch.object(fence, "run", side_effect=outputs), \
             mock.patch.object(fence, "systemd_unit_root_record", side_effect=absent):
            snapshot = fence.systemd_unit_path_snapshot()
        self.assertEqual(snapshot["manager"], manager.split())
        self.assertEqual(snapshot["analyzed"], list(fence.SYSTEMD_UNIT_PATH_ALLOWLIST))

        manager_after = " ".join(manager.split()[1:])
        outputs = [mock.Mock(stdout=value) for value in
                   (manager, analyzed, manager_after, analyzed)]
        with mock.patch.object(fence, "run", side_effect=outputs), \
             mock.patch.object(fence, "systemd_unit_root_record", side_effect=absent):
            with self.assertRaisesRegex(RuntimeError, "changed while"):
                fence.systemd_unit_path_snapshot()

    def test_systemd_unit_path_parser_rejects_untrusted_duplicate_order_and_missing_root(self):
        valid = ["/etc/systemd/system", "/run/systemd/system",
                 "/usr/lib/systemd/system"]
        with self.assertRaisesRegex(RuntimeError, "duplicated"):
            fence.parse_systemd_unit_paths(" ".join(valid + [valid[-1]]), "fixture")
        with self.assertRaisesRegex(RuntimeError, "outside"):
            fence.parse_systemd_unit_paths(
                " ".join(valid[:-1] + ["/opt/untrusted/systemd"]), "fixture")
        with self.assertRaisesRegex(RuntimeError, "precedence"):
            fence.parse_systemd_unit_paths(" ".join(reversed(valid)), "fixture")
        with self.assertRaisesRegex(RuntimeError, "omit"):
            fence.parse_systemd_unit_paths(
                "/etc/systemd/system /usr/lib/systemd/system", "fixture")

    def test_systemd_unit_path_root_rejects_regular_file_and_writable_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            regular = root / "not-a-root"
            regular.write_text("fixture")
            with self.assertRaisesRegex(RuntimeError, "real directory"):
                fence.systemd_unit_root_record(str(regular))
            unsafe = mock.Mock(st_mode=stat.S_IFDIR | 0o777, st_uid=0, st_gid=0,
                               st_dev=1, st_ino=2)
            with mock.patch.object(fence.os, "name", "posix"), \
                 mock.patch.object(fence.os, "lstat", return_value=unsafe):
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    fence.systemd_unit_root_record(str(root))

    def test_current_unit_paths_allow_volatile_recreation_but_reject_static_inode_change(self):
        roots = ["/etc/systemd/system", "/run/systemd/transient",
                 "/run/systemd/system", "/usr/lib/systemd/system"]

        def snapshot(inodes):
            return {"schema_version": 1, "manager": list(roots),
                    "analyzed": list(roots),
                    "roots": [{"path": path, "kind": "directory",
                               "lstat": {"device": 1, "inode": inode,
                                         "mode": stat.S_IFDIR | 0o755,
                                         "uid": 0, "gid": 0}}
                              for path, inode in zip(roots, inodes)]}

        initial = snapshot([10, 20, 30, 40])
        volatile_changed = snapshot([10, 21, 30, 40])
        with mock.patch.object(fence, "systemd_unit_path_snapshot",
                               return_value=volatile_changed):
            self.assertEqual(fence.current_systemd_unit_paths(initial), volatile_changed)
        initial_absent = snapshot([10, 20, 30, 40])
        initial_absent["roots"][1] = {"path": roots[1], "kind": "absent"}
        with mock.patch.object(fence, "systemd_unit_path_snapshot",
                               return_value=volatile_changed):
            self.assertEqual(fence.current_systemd_unit_paths(initial_absent),
                             volatile_changed)
        static_changed = snapshot([11, 20, 30, 40])
        with mock.patch.object(fence, "systemd_unit_path_snapshot",
                               return_value=static_changed):
            with self.assertRaisesRegex(RuntimeError, "static systemd"):
                fence.current_systemd_unit_paths(initial)

    def test_systemd_candidate_enumeration_covers_wants_run_and_vendor_aliases(self):
        etc = pathlib.Path("C:/fixture/etc-systemd")
        run_root = pathlib.Path("C:/fixture/run-systemd")
        vendor = pathlib.Path("C:/fixture/usr-lib-systemd")
        wants_dir = etc / "multi-user.target.wants"
        wants = wants_dir / fence.DRAMA_UNIT
        run_alias = run_root / "runtime-alias.service"
        vendor_alias = vendor / "vendor-alias.service"
        entries = {str(etc): [wants_dir], str(wants_dir): [wants],
                   str(run_root): [run_alias], str(vendor): [vendor_alias]}
        records = {str(path): synthetic_link_record(path, fence.DRAMA_LOCAL_FRAGMENT)
                   for path in (wants, run_alias, vendor_alias)}
        snapshot = {"roots": [{"path": str(root), "kind": "directory"}
                              for root in (etc, run_root, vendor)]}
        with mock.patch.object(fence, "systemd_unit_root_record",
                               side_effect=lambda value: {"path": value,
                                                          "kind": "directory"}), \
             mock.patch.object(fence, "stable_systemd_directory_entries",
                               side_effect=lambda path: entries.get(str(path), [])), \
             mock.patch.object(fence, "path_lexists", return_value=True), \
             mock.patch.object(type(wants_dir), "is_dir", return_value=True), \
             mock.patch.object(fence, "path_is_symlink",
                               side_effect=lambda path: str(path) in records), \
             mock.patch.object(fence, "symlink_identity_record",
                               side_effect=lambda path: records[str(path)]):
            found = fence.systemd_candidate_link_records(snapshot)
        self.assertEqual([row["path"] for row in found], sorted(records))

    def test_systemd_alias_chain_detects_vendor_run_and_outside_unit_paths(self):
        fragment = pathlib.Path("C:/fixture/etc-systemd") / fence.DRAMA_UNIT
        etc = fragment.parent
        run_root = pathlib.Path("C:/fixture/run-systemd")
        vendor = pathlib.Path("C:/fixture/usr-lib-systemd")
        outside = pathlib.Path("C:/fixture/dracut/modules") / "alias.service"
        outside_second = pathlib.Path("C:/fixture/lib/systemd") / "second-alias.service"
        cases = [
            (etc / "multi-user.target.wants" / fence.DRAMA_UNIT,
             [vendor / "vendor-alias.service"]),
            (run_root / "runtime-alias.service", [outside, outside_second]),
        ]
        for entry, middles in cases:
            with self.subTest(entry=str(entry)):
                paths = [entry] + middles
                records = {str(path): synthetic_link_record(
                    path, paths[index + 1] if index + 1 < len(paths) else fragment)
                           for index, path in enumerate(paths)}
                first = records[str(entry)]
                with mock.patch.object(fence, "path_lexists",
                                       side_effect=lambda path: str(path) in records), \
                     mock.patch.object(fence, "path_is_symlink",
                                       side_effect=lambda path: str(path) in records), \
                     mock.patch.object(fence, "stable_target_parent_resolution",
                                       side_effect=synthetic_parent_resolution), \
                     mock.patch.object(fence, "symlink_identity_record",
                                       side_effect=lambda path: records[str(path)]):
                    found = fence.resolve_systemd_symlink_chain(
                        first, fragment, [etc, run_root, vendor])
                self.assertEqual([hop["path"] for hop in found["chain"]],
                                 [str(path) for path in paths])

    def test_systemd_alias_chain_cycle_is_fatal_but_unrelated_broken_link_is_ignored(self):
        root = pathlib.Path("C:/fixture/etc-systemd")
        fragment = root / fence.DRAMA_UNIT
        entry = root / "cycle-a.service"
        middle = root / "cycle-b.service"
        first = synthetic_link_record(entry, middle)
        second = synthetic_link_record(middle, entry)
        records = {str(entry): first, str(middle): second}
        with mock.patch.object(fence, "path_lexists",
                               side_effect=lambda path: str(path) in records), \
             mock.patch.object(fence, "path_is_symlink",
                               side_effect=lambda path: str(path) in records), \
             mock.patch.object(fence, "stable_target_parent_resolution",
                               side_effect=synthetic_parent_resolution), \
             mock.patch.object(fence, "symlink_identity_record",
                               side_effect=lambda path: records[str(path)]):
            with self.assertRaisesRegex(RuntimeError, "cycle"):
                fence.resolve_systemd_symlink_chain(first, fragment, [root])

        self_link = synthetic_link_record(root / "self.service", root / "self.service")
        with mock.patch.object(fence, "path_lexists", return_value=True), \
             mock.patch.object(fence, "path_is_symlink", return_value=True), \
             mock.patch.object(fence, "stable_target_parent_resolution",
                               side_effect=synthetic_parent_resolution), \
             mock.patch.object(fence, "symlink_identity_record",
                               return_value=self_link):
            with self.assertRaisesRegex(RuntimeError, "cycle"):
                fence.resolve_systemd_symlink_chain(self_link, fragment, [root])

        broken = synthetic_link_record(root / "broken.service",
                                       pathlib.Path("C:/missing/vendor.service"))
        with mock.patch.object(fence, "path_lexists", return_value=False), \
             mock.patch.object(fence, "stable_target_parent_resolution",
                               side_effect=synthetic_parent_resolution):
            self.assertIsNone(fence.resolve_systemd_symlink_chain(
                broken, fragment, [root]))
        middle_broken = synthetic_link_record(root / "middle-broken.service",
                                              pathlib.Path("C:/missing/final.service"))
        first_broken = synthetic_link_record(root / "first-broken.service",
                                             pathlib.Path(middle_broken["path"]))
        broken_records = {middle_broken["path"]: middle_broken,
                          first_broken["path"]: first_broken}
        with mock.patch.object(fence, "path_lexists",
                               side_effect=lambda path: str(path) in broken_records), \
             mock.patch.object(fence, "path_is_symlink",
                               side_effect=lambda path: str(path) in broken_records), \
             mock.patch.object(fence, "stable_target_parent_resolution",
                               side_effect=synthetic_parent_resolution), \
             mock.patch.object(fence, "symlink_identity_record",
                               side_effect=lambda path: broken_records[str(path)]):
            self.assertIsNone(fence.resolve_systemd_symlink_chain(
                first_broken, fragment, [root]))

    def test_systemd_alias_chain_detects_fragment_before_following_new_mask(self):
        root = pathlib.Path("C:/fixture/etc-systemd")
        fragment = root / fence.DRAMA_UNIT
        entry = root / "multi-user.target.wants" / fence.DRAMA_UNIT
        first = synthetic_link_record(entry, fragment)
        fragment_mask = synthetic_link_record(fragment, pathlib.Path("/dev/null"))
        records = {str(entry): first, str(fragment): fragment_mask}
        calls = []

        def identity(path):
            calls.append(str(path))
            return records[str(path)]

        with mock.patch.object(fence, "path_lexists", return_value=True), \
             mock.patch.object(fence, "path_is_symlink", return_value=True), \
             mock.patch.object(fence, "stable_target_parent_resolution",
                               side_effect=synthetic_parent_resolution), \
             mock.patch.object(fence, "symlink_identity_record", side_effect=identity):
            found = fence.resolve_systemd_symlink_chain(first, fragment, [root])
        self.assertEqual(found["terminal_path"], str(fragment))
        self.assertEqual(calls, [str(entry)])

    def test_parent_directory_alias_hits_missing_fragment_before_leaf_lookup(self):
        root = fixture_absolute_path("unit-root")
        fragment = fixture_absolute_path("canonical", "etc", "systemd", fence.DRAMA_UNIT)
        entry = root / "multi-user.target.wants" / fence.DRAMA_UNIT
        directory_link_path = fixture_absolute_path("some", "dir-link")
        first = synthetic_link_record(entry, directory_link_path / fence.DRAMA_UNIT)
        directory_link = synthetic_link_record(directory_link_path, fragment.parent)

        def component(path):
            if path == directory_link_path:
                return {"path": str(path), "kind": "symlink",
                        "symlink": directory_link}
            return {"path": str(path), "kind": "directory"}

        with mock.patch.object(fence, "parent_path_component_record",
                               side_effect=component), \
             mock.patch.object(fence, "symlink_identity_record", return_value=first), \
             mock.patch.object(fence, "path_lexists",
                               side_effect=AssertionError("leaf existence must not be queried")):
            found = fence.resolve_systemd_symlink_chain(first, fragment, [root])
        self.assertEqual(found["terminal_path"], str(fragment))
        parent = found["chain"][0]["target_parent_resolution"]
        self.assertEqual(parent["canonical_leaf_path"], str(fragment))
        self.assertEqual(parent["directory_symlinks"], [directory_link])

    def test_parent_directory_alias_hits_fragment_before_following_direct_mask(self):
        root = fixture_absolute_path("unit-root")
        fragment = fixture_absolute_path("canonical", "etc", "systemd", fence.DRAMA_UNIT)
        entry = root / "runtime-alias.service"
        directory_link_path = fixture_absolute_path("some", "dir-link")
        first = synthetic_link_record(entry, directory_link_path / fence.DRAMA_UNIT)
        directory_link = synthetic_link_record(directory_link_path, fragment.parent)
        fragment_mask = synthetic_link_record(fragment, pathlib.Path("/dev/null"))
        identity_calls = []

        def component(path):
            if path == directory_link_path:
                return {"path": str(path), "kind": "symlink",
                        "symlink": directory_link}
            return {"path": str(path), "kind": "directory"}

        def identity(path):
            identity_calls.append(str(path))
            if path == fragment:
                return fragment_mask
            return first

        with mock.patch.object(fence, "parent_path_component_record",
                               side_effect=component), \
             mock.patch.object(fence, "symlink_identity_record", side_effect=identity), \
             mock.patch.object(fence, "path_lexists", return_value=True) as lexists, \
             mock.patch.object(fence, "path_is_symlink", return_value=True) as is_link:
            found = fence.resolve_systemd_symlink_chain(first, fragment, [root])
        self.assertEqual(found["terminal_path"], str(fragment))
        self.assertNotIn(str(fragment), identity_calls)
        lexists.assert_not_called()
        is_link.assert_not_called()

    def test_parent_directory_resolution_supports_relative_multihop(self):
        target = fixture_absolute_path("some", "first", fence.DRAMA_UNIT)
        first_path = target.parent
        alias_path = fixture_absolute_path("some", "alias")
        canonical_parent = fixture_absolute_path("canonical", "etc", "systemd")
        first = synthetic_link_record(first_path, alias_path)
        first["link_target"] = "../alias"
        first["link_target_sha256"] = hashlib.sha256(b"../alias").hexdigest()
        second = synthetic_link_record(alias_path, canonical_parent)

        def component(path):
            if path == first_path:
                return {"path": str(path), "kind": "symlink", "symlink": first}
            if path == alias_path:
                return {"path": str(path), "kind": "symlink", "symlink": second}
            return {"path": str(path), "kind": "directory"}

        with mock.patch.object(fence, "parent_path_component_record",
                               side_effect=component):
            resolution = fence.stable_target_parent_resolution(str(target))
        self.assertEqual(resolution["canonical_leaf_path"],
                         str(canonical_parent / fence.DRAMA_UNIT))
        self.assertEqual(resolution["directory_symlinks"], [first, second])

    def test_parent_directory_resolution_cycle_and_read_race_fail_closed(self):
        target = fixture_absolute_path("some", "a", fence.DRAMA_UNIT)
        first_path = target.parent
        second_path = fixture_absolute_path("some", "b")
        first = synthetic_link_record(first_path, second_path)
        second = synthetic_link_record(second_path, first_path)

        def cycle_component(path):
            if path == first_path:
                return {"path": str(path), "kind": "symlink", "symlink": first}
            if path == second_path:
                return {"path": str(path), "kind": "symlink", "symlink": second}
            return {"path": str(path), "kind": "directory"}

        with mock.patch.object(fence, "parent_path_component_record",
                               side_effect=cycle_component):
            with self.assertRaisesRegex(RuntimeError, "cycle"):
                fence.stable_target_parent_resolution(str(target))

        calls = [0]

        def racing_component(path):
            if path == first_path:
                calls[0] += 1
                if calls[0] > 1:
                    raise OSError("parent readlink race")
                return {"path": str(path), "kind": "symlink", "symlink": first}
            return {"path": str(path), "kind": "directory"}

        with mock.patch.object(fence, "parent_path_component_record",
                               side_effect=racing_component):
            with self.assertRaisesRegex(OSError, "parent readlink race"):
                fence.stable_target_parent_resolution(str(target))

    def test_systemd_alias_chain_read_error_and_outside_entry_fail_closed(self):
        root = pathlib.Path("C:/fixture/etc-systemd")
        fragment = root / fence.DRAMA_UNIT
        entry = root / "alias.service"
        middle = root / "middle.service"
        first = synthetic_link_record(entry, middle)
        with mock.patch.object(fence, "path_lexists", return_value=True), \
             mock.patch.object(fence, "path_is_symlink", return_value=True), \
             mock.patch.object(fence, "stable_target_parent_resolution",
                               side_effect=synthetic_parent_resolution), \
             mock.patch.object(fence, "symlink_identity_record",
                               side_effect=OSError("readlink race")):
            with self.assertRaisesRegex(OSError, "readlink race"):
                fence.resolve_systemd_symlink_chain(first, fragment, [root])
        outside = synthetic_link_record(pathlib.Path("C:/outside/alias.service"), fragment)
        with self.assertRaisesRegex(RuntimeError, "outside the validated"):
            fence.resolve_systemd_symlink_chain(outside, fragment, [root])

    def test_loaded_absent_wants_requires_exact_absolute_target(self):
        fragment = pathlib.Path("C:/systemd/drama-material-api.service")
        link = pathlib.Path("C:/systemd/multi-user.target.wants/drama-material-api.service")
        record = {"path": str(link), "kind": "dangling-symlink",
                  "link_target": "../drama-material-api.service",
                  "target_path": str(fragment), "resolved_path": str(fragment)}
        with mock.patch.object(fence, "DRAMA_WANTS_LINK", link), \
             mock.patch.object(fence, "drama_fragment_symlink_records", return_value=[record]), \
             mock.patch.object(fence, "path_lexists", side_effect=lambda path: path == link), \
             mock.patch.object(fence, "path_is_symlink", return_value=True), \
             mock.patch.object(fence.os, "readlink", return_value="../drama-material-api.service"):
            with self.assertRaisesRegex(RuntimeError, "expected absolute"):
                fence.drama_wants_link_record(fragment, {})

    def test_loaded_absent_wants_accepts_only_unique_direct_absolute_chain(self):
        fragment = pathlib.Path("C:/systemd/drama-material-api.service")
        link = pathlib.Path("C:/systemd/multi-user.target.wants/drama-material-api.service")
        record = synthetic_link_record(link, fragment)
        record["target_parent_resolution"] = synthetic_parent_resolution(fragment)
        hop = dict(record)
        record.update({"chain_schema_version": 1, "chain": [hop],
                       "terminal_path": str(fragment)})
        with mock.patch.object(fence, "DRAMA_WANTS_LINK", link), \
             mock.patch.object(fence, "drama_fragment_symlink_records",
                               return_value=[record]), \
             mock.patch.object(fence, "path_lexists",
                               side_effect=lambda path: path == link), \
             mock.patch.object(fence, "path_is_symlink", return_value=True):
            self.assertEqual(fence.drama_wants_link_record(fragment, {}), record)

    def test_loaded_absent_wants_rejects_any_additional_alias_entry(self):
        fragment = fence.DRAMA_LOCAL_FRAGMENT
        wants = {"path": str(fence.DRAMA_WANTS_LINK), "kind": "dangling-symlink",
                 "link_target": str(fragment), "target_path": str(fragment),
                 "resolved_path": str(fragment)}
        alias = dict(wants, path="/run/systemd/system/unexpected-alias.service")
        with mock.patch.object(fence, "drama_fragment_symlink_records",
                               return_value=[wants, alias]), \
             mock.patch.object(fence, "path_lexists",
                               side_effect=lambda path: path == fence.DRAMA_WANTS_LINK), \
             mock.patch.object(fence, "path_is_symlink", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "scope changed"):
                fence.drama_wants_link_record(fragment, {})

    def test_fragment_link_enumeration_includes_aliases_outside_wants(self):
        wants = {"path": str(fence.DRAMA_WANTS_LINK),
                 "target_path": str(fence.DRAMA_LOCAL_FRAGMENT)}
        alias = {"path": "/run/systemd/system/unexpected-alias.service",
                 "target_path": str(fence.DRAMA_LOCAL_FRAGMENT)}
        with mock.patch.object(fence, "systemd_candidate_link_records",
                               side_effect=[[alias, wants], [alias, wants]]), \
             mock.patch.object(fence, "resolve_systemd_symlink_chain",
                               side_effect=lambda row, fragment, roots: row):
            result = fence.drama_fragment_symlink_records(
                fence.DRAMA_LOCAL_FRAGMENT,
                {"roots": [{"path": "/etc/systemd/system", "kind": "directory"}]})
        self.assertEqual([row["path"] for row in result], sorted(
            [alias["path"], wants["path"]]))

    def test_disable_nonzero_is_accepted_only_when_exact_enablement_is_removed(self):
        result = mock.Mock(returncode=1)
        unit_paths = {"schema_version": 1}
        with mock.patch.object(fence, "current_systemd_unit_paths",
                               return_value=unit_paths) as paths, \
             mock.patch.object(fence, "drama_wants_link_record",
                               side_effect=[{"kind": "dangling-symlink"}, {"kind": "absent"},
                                            {"kind": "absent"}]), \
             mock.patch.object(fence, "run", return_value=result) as run:
            outcome = fence.remove_loaded_absent_enablement(unit_paths)
        self.assertEqual(outcome, {"attempted": True, "rc": 1, "postcondition": "absent"})
        self.assertEqual(paths.call_count, 3)
        run.assert_called_once_with(
            ["systemctl", "disable", "--no-reload", fence.DRAMA_UNIT], check=False)
        with mock.patch.object(fence, "current_systemd_unit_paths",
                               return_value=unit_paths), \
             mock.patch.object(fence, "drama_wants_link_record",
                               side_effect=[{"kind": "dangling-symlink"},
                                            {"kind": "dangling-symlink"}]), \
             mock.patch.object(fence, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "remains after disable"):
                fence.remove_loaded_absent_enablement(unit_paths)

    def test_source_directory_identity_allows_0755_ancestors_and_rejects_writable(self):
        safe = mock.Mock(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0)
        fence.validate_source_directory_identity(safe, require_private=False)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            fence.validate_source_directory_identity(
                mock.Mock(st_mode=stat.S_IFDIR | 0o775, st_uid=0, st_gid=0),
                require_private=False)
        with self.assertRaisesRegex(RuntimeError, "0700"):
            fence.validate_source_directory_identity(safe, require_private=True)

    def test_source_fence_lock_is_nonblocking_and_rejects_concurrent_operator(self):
        fake_fcntl = mock.Mock()
        fake_fcntl.LOCK_EX = 2
        fake_fcntl.LOCK_NB = 4
        fake_fcntl.flock.side_effect = OSError("busy")
        identity = mock.Mock(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_gid=0)
        with mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}), \
             mock.patch.object(fence.os, "name", "posix"), \
             mock.patch.object(fence.os, "open", return_value=7), \
             mock.patch.object(fence.os, "fchmod"), \
             mock.patch.object(fence.os, "fstat", return_value=identity), \
             mock.patch.object(fence.os, "close") as close:
            with self.assertRaisesRegex(RuntimeError, "holds the lock"):
                fence.acquire_source_lock()
        close.assert_called_once_with(7)

    def test_source_storage_guard_rejects_parent_symlink(self):
        data = pathlib.Path("C:/fixture-data")
        base = data / "migrations" / fence.RUN_ID / "source-fence"
        with mock.patch.object(fence, "DATA_ROOT", data), \
             mock.patch.object(fence, "BASE", base), \
             mock.patch.object(fence, "path_lexists", return_value=True), \
             mock.patch.object(fence, "path_is_symlink",
                               side_effect=lambda path: path == data / "migrations"), \
             mock.patch.object(fence, "run"):
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                fence.source_storage_guard(create=False)

    def test_private_artifacts_and_retirement_fsync_parent_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            evidence = root / "evidence"
            evidence.mkdir()
            target = evidence / "snapshot.json"
            with mock.patch.object(fence, "fsync_directory") as sync:
                fence.write_private_json(target, {"ok": True})
            sync.assert_called_once_with(evidence)

            private_dir = evidence / "unit-backup"
            with mock.patch.object(fence, "fsync_directory") as sync:
                fence.mkdir_private(private_dir)
            sync.assert_called_once_with(evidence)

            local = root / "drama.service"
            local.write_bytes(b"[Service]\n")
            backup = root / "backup"
            backup.mkdir(mode=0o700)
            with mock.patch.object(fence, "fsync_directory") as sync:
                fence.retire_local_unit(local, backup)
            synced = [call[0][0] for call in sync.call_args_list]
            self.assertIn(backup, synced)
            self.assertIn(root, synced)

    def test_disable_fsync_failure_is_fatal_before_masking(self):
        result = mock.Mock(returncode=0)
        unit_paths = {"schema_version": 1}
        with mock.patch.object(fence, "current_systemd_unit_paths",
                               return_value=unit_paths), \
             mock.patch.object(fence, "drama_wants_link_record",
                               side_effect=[{"kind": "dangling-symlink"},
                                            {"kind": "absent"}]), \
             mock.patch.object(fence, "run", return_value=result), \
             mock.patch.object(fence, "fsync_directory",
                               side_effect=OSError("directory sync failed")):
            with self.assertRaisesRegex(OSError, "directory sync failed"):
                fence.remove_loaded_absent_enablement(unit_paths)

    def test_lock_is_released_when_snapshot_or_resume_preparation_fails(self):
        handle = mock.Mock()
        checkpoint = mock.Mock()
        checkpoint.read_text.return_value = json.dumps({
            "group": "x", "run_id": fence.RUN_ID,
            "checked_at_epoch": __import__("time").time(),
            "new_admission_closed": True, "triggers_paused": True,
            "cpu_drained": True, "no_unknown_repairs": True})
        checkpoint.read_bytes.return_value = b"checkpoint"
        states = [{"unit": unit, "pid": 0, "active": "inactive",
                   "substate": "dead", "enabled": "enabled", "fragment": ""}
                  for unit in fence.GROUPS["x"]]
        with mock.patch.object(sys, "argv", ["source_fence.py", "x", "--checkpoint",
                                              "checkpoint.json", "--apply", "--resume"]), \
             mock.patch.object(fence.socket, "gethostname", return_value="VM-0-13-centos"), \
             mock.patch.object(fence.pathlib.Path, "read_text",
                               return_value=checkpoint.read_text()), \
             mock.patch.object(fence.pathlib.Path, "read_bytes", return_value=b"checkpoint"), \
             mock.patch.object(fence, "source_storage_guard"), \
             mock.patch.object(fence, "inspect", side_effect=states), \
             mock.patch.object(fence, "acquire_source_lock", return_value=handle), \
             mock.patch.object(fence, "apply_locked_source_fence",
                               side_effect=RuntimeError("resume snapshot invalid")):
            with self.assertRaisesRegex(RuntimeError, "resume snapshot invalid"):
                fence.main()
        handle.close.assert_called_once_with()

    def test_drama_source_failure_closes_only_retired_api(self):
        original = ValueError("fixture failure")
        with tempfile.TemporaryDirectory() as folder:
            backup = pathlib.Path(folder)
            (backup / "original.service").write_text("[Service]\n")
            with mock.patch.object(fence, "best_effort_command",
                                   return_value={"rc": 0, "error_type": None}) as command, \
                 mock.patch.object(fence, "verified_definition_backup", return_value=archived_capability()), \
                 mock.patch.object(fence, "closure_state", return_value=({"active": "inactive"}, True, None)), \
                 mock.patch.object(fence, "shared_tunnel_snapshot", return_value={"pid": 456}), \
                 mock.patch.object(fence, "write_failure_evidence") as evidence:
                with self.assertRaisesRegex(RuntimeError, "HIGH RISK"):
                    fence.fail_closed_drama("mask-source", {"pid": 456}, original, backup)
        self.assertEqual([call[0][0] for call in command.call_args_list], [
            ["systemctl", "stop", "drama-material-api.service"],
        ])
        self.assertFalse(any(fence.DRAMA_SHARED_TUNNEL in str(call) for call in command.call_args_list))
        call = evidence.call_args
        self.assertEqual(call[0][0:2], ("mask-source", "ValueError"))
        self.assertFalse(call[0][4])

    def test_drama_source_failure_raises_high_risk_when_closure_unproven(self):
        original = ValueError("fixture failure")
        with tempfile.TemporaryDirectory() as folder:
            backup = pathlib.Path(folder)
            (backup / "original.service").write_text("[Service]\n")
            with mock.patch.object(fence, "best_effort_command",
                                   return_value={"rc": 1, "error_type": None}), \
                 mock.patch.object(fence, "verified_definition_backup", return_value=archived_capability()), \
                 mock.patch.object(fence, "closure_state",
                                   return_value=({"active": "active", "pid": 99}, False, None)), \
                 mock.patch.object(fence, "shared_tunnel_snapshot", return_value={"pid": 456}), \
                 mock.patch.object(fence, "write_failure_evidence") as evidence:
                with self.assertRaisesRegex(RuntimeError, "HIGH RISK") as raised:
                    fence.fail_closed_drama("stop-source", {"pid": 456}, original, backup)
        self.assertIs(raised.exception.__cause__, original)
        self.assertFalse(evidence.call_args[0][4])
        commands = evidence.call_args[0][2]
        self.assertEqual(commands["stop"]["rc"], 1)
        self.assertNotIn("disable", commands)

    def test_drama_source_failure_evidence_error_preserves_original_cause(self):
        for final_closed in (False, True):
            original = ValueError("fixture failure")
            with tempfile.TemporaryDirectory() as folder:
                backup = pathlib.Path(folder)
                with mock.patch.object(fence, "best_effort_command",
                                       return_value={"rc": 0, "error_type": None}), \
                     mock.patch.object(fence, "verified_definition_backup",
                                       return_value=archived_capability()), \
                     mock.patch.object(fence, "closure_state",
                                       return_value=({"active": "inactive"}, final_closed, None)), \
                     mock.patch.object(fence, "shared_tunnel_snapshot",
                                       return_value={"pid": 456}), \
                     mock.patch.object(fence, "write_failure_evidence",
                                       side_effect=OSError("evidence fixture failure")):
                    with self.assertRaisesRegex(RuntimeError, "HIGH RISK") as raised:
                        fence.fail_closed_drama("mask-source", {"pid": 456}, original, backup)
            self.assertIs(raised.exception.__cause__, original)

    def test_drama_closure_state_requires_zero_ports_and_successful_probe(self):
        values = {"ActiveState": "inactive", "SubState": "dead",
                  "UnitFileState": "masked", "LoadState": "masked",
                  "MainPID": "0", "ControlPID": "0",
                  "ControlGroup": ""}
        with mock.patch.object(fence, "prop", side_effect=lambda unit, name: values[name]), \
             mock.patch.object(fence, "is_persistent_mask", return_value=True), \
             mock.patch.object(fence, "port_rows", return_value=[]):
            state, closed, error = fence.closure_state(require_masked=True)
        self.assertTrue(closed)
        self.assertIsNone(error)
        self.assertEqual(state["port_8787_listener_count"], 0)
        self.assertEqual(state["port_8787_established_count"], 0)

        with mock.patch.object(fence, "prop", side_effect=lambda unit, name: values[name]), \
             mock.patch.object(fence, "is_persistent_mask", return_value=True), \
             mock.patch.object(fence, "port_rows", side_effect=[["LISTEN 8787"], []]):
            state, closed, error = fence.closure_state(require_masked=True)
        self.assertFalse(closed)
        self.assertIsNone(error)
        self.assertEqual(state["port_8787_listener_count"], 1)

        with mock.patch.object(fence, "prop", side_effect=lambda unit, name: values[name]), \
             mock.patch.object(fence, "is_persistent_mask", return_value=True), \
             mock.patch.object(fence, "port_rows", side_effect=[[], ["ESTAB 8787"]]):
            state, closed, error = fence.closure_state(require_masked=True)
        self.assertFalse(closed)
        self.assertIsNone(error)
        self.assertEqual(state["port_8787_established_count"], 1)

        with mock.patch.object(fence, "prop", side_effect=lambda unit, name: values[name]), \
             mock.patch.object(fence, "is_persistent_mask", return_value=True), \
             mock.patch.object(fence, "port_rows", side_effect=RuntimeError("ss failed")):
            _state, closed, error = fence.closure_state(require_masked=True)
        self.assertFalse(closed)
        self.assertEqual(error, "RuntimeError")

    def test_source_main_drama_success_stops_masks_and_verifies_final_state(self):
        shared = complete_shared_tunnel()
        definition = {"fragment": {"path": "/etc/systemd/system/drama-material-api.service",
                                     "kind": "file", "content_sha256": "b" * 64},
                      "dropins": [], "definition_sha256": "c" * 64}
        runtime = {"active": True, "enabled": "enabled"}

        def inspect(unit):
            if runtime["active"]:
                return {"unit": unit, "pid": 123, "active": "active", "substate": "running",
                        "enabled": runtime["enabled"], "control_pid": 0,
                        "control_group": "/system.slice/drama-material-api.service",
                        "fragment": definition["fragment"]["path"], "threads": 1, "children": []}
            return {"unit": unit, "pid": 0, "active": "inactive", "substate": "dead",
                    "enabled": runtime["enabled"],
                    "load_state": "masked" if runtime["enabled"] == "masked" else "loaded",
                    "control_pid": 0, "control_group": "", "cgroup_pids": [],
                    "fragment": ""}

        def prop(unit, name):
            values = {"UnitFileState": runtime["enabled"], "MainPID": "123" if runtime["active"] else "0",
                      "ActiveState": "active" if runtime["active"] else "inactive",
                      "LoadState": "masked" if runtime["enabled"] == "masked" else "loaded"}
            return values[name]

        def command(args, check=True):
            if args[:2] == ["systemctl", "stop"]:
                runtime["active"] = False
            elif args[:2] == ["systemctl", "disable"]:
                runtime["enabled"] = "disabled"
            elif args[:2] == ["systemctl", "mask"]:
                runtime["enabled"] = "masked"
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder) / "source-fence"
            base.mkdir()
            checkpoint = pathlib.Path(folder) / "checkpoint.json"
            checkpoint.write_text(json.dumps({"group": "drama", "run_id": fence.RUN_ID,
                                              "checked_at_epoch": __import__("time").time(),
                                              "new_admission_closed": True, "triggers_paused": True,
                                              "cpu_drained": True, "no_unknown": True}))

            def backup(state, unit_backup, resume=False):
                (unit_backup / "original.service").write_text("[Service]\n")
                fence.write_private_json(unit_backup / "definition.json", state["definition"])

            with mock.patch.object(sys, "argv", ["source_fence.py", "drama", "--checkpoint",
                                                  str(checkpoint), "--apply"]), \
                 mock.patch.object(fence.socket, "gethostname", return_value="VM-0-13-centos"), \
                 mock.patch.object(fence, "BASE", base), \
                 mock.patch.object(fence, "source_storage_guard"), \
                 mock.patch.object(fence, "inspect", side_effect=inspect), \
                 mock.patch.object(fence, "unit_definition_snapshot", return_value=definition), \
                 mock.patch.object(fence, "shared_tunnel_snapshot", return_value=shared), \
                 mock.patch.object(fence, "port_rows", return_value=[]), \
                 mock.patch.object(fence, "validate_drama_checkpoint"), \
                 mock.patch.object(fence, "backup_unit_definition", side_effect=backup), \
                 mock.patch.object(fence, "verified_definition_backup", return_value=archived_capability()), \
                 mock.patch.object(fence, "validate_recorded_definition_current"), \
                 mock.patch.object(fence, "closure_state",
                                   return_value=({"active": "inactive"}, True, None)), \
                 mock.patch.object(fence, "is_persistent_mask", return_value=True), \
                 mock.patch.object(fence, "write_drama_success_evidence") as success, \
                 mock.patch.object(fence, "prop", side_effect=prop), \
                 mock.patch.object(fence, "run", side_effect=command) as run:
                fence.main()
        self.assertEqual(runtime, {"active": False, "enabled": "masked"})
        commands = [call[0][0] for call in run.call_args_list]
        self.assertEqual(commands, [["systemctl", "stop", fence.DRAMA_UNIT],
                                    ["systemctl", "disable", "--no-reload", fence.DRAMA_UNIT],
                                    ["systemctl", "mask", "--no-reload", fence.DRAMA_UNIT],
                                    ["systemctl", "daemon-reload"],
                                    ["systemctl", "stop", fence.DRAMA_UNIT]])
        success.assert_called_once()

    def test_source_main_loaded_absent_happy_path_never_retires_fragment(self):
        shared = complete_shared_tunnel()
        definition = loaded_absent_definition()
        runtime = {"active": True, "enabled": "enabled", "load_state": "loaded",
                   "wants": True, "masked": False}

        def inspect(unit):
            if runtime["active"]:
                return {"unit": unit, "pid": 123, "active": "active", "substate": "running",
                        "enabled": runtime["enabled"], "load_state": runtime["load_state"],
                        "control_pid": 0, "control_group": "/system.slice/" + unit,
                        "fragment": str(fence.DRAMA_LOCAL_FRAGMENT), "threads": 1,
                        "children": [], "pid_start_ticks": 8001}
            return {"unit": unit, "pid": 0, "active": "inactive", "substate": "dead",
                    "enabled": runtime["enabled"], "load_state": runtime["load_state"],
                    "control_pid": 0, "control_group": "", "cgroup_pids": [],
                    "fragment": "" if runtime["masked"] else str(fence.DRAMA_LOCAL_FRAGMENT)}

        def prop(unit, name):
            return {"UnitFileState": runtime["enabled"],
                    "LoadState": runtime["load_state"],
                    "MainPID": "123" if runtime["active"] else "0",
                    "ActiveState": "active" if runtime["active"] else "inactive",
                    "SubState": "running" if runtime["active"] else "dead",
                    "ControlPID": "0",
                    "ControlGroup": "/system.slice/" + unit if runtime["active"] else ""}[name]

        def command(args, check=True):
            if args[:2] == ["systemctl", "stop"]:
                runtime["active"] = False
            elif args[:2] == ["systemctl", "disable"]:
                runtime["wants"] = False
                runtime["enabled"] = "disabled"
            elif args[:2] == ["systemctl", "mask"]:
                runtime["masked"] = True
                runtime["enabled"] = "masked"
            elif args == ["systemctl", "daemon-reload"]:
                runtime["load_state"] = "masked"
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder) / "source-fence"
            base.mkdir()
            checkpoint = pathlib.Path(folder) / "checkpoint.json"
            checkpoint.write_text(json.dumps({"group": "drama", "run_id": fence.RUN_ID,
                                              "checked_at_epoch": __import__("time").time(),
                                              "new_admission_closed": True,
                                              "triggers_paused": True, "cpu_drained": True,
                                              "no_unknown": True}))

            def backup(state, unit_backup, resume=False):
                fence.write_private_json(unit_backup / "definition.json", definition)

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    sys, "argv", ["source_fence.py", "drama", "--checkpoint",
                                  str(checkpoint), "--apply"]))
                stack.enter_context(mock.patch.object(
                    fence.socket, "gethostname", return_value="VM-0-13-centos"))
                stack.enter_context(mock.patch.object(fence, "BASE", base))
                stack.enter_context(mock.patch.object(fence, "source_storage_guard"))
                stack.enter_context(mock.patch.object(fence, "inspect", side_effect=inspect))
                stack.enter_context(mock.patch.object(
                    fence, "unit_definition_snapshot", return_value=definition))
                stack.enter_context(mock.patch.object(
                    fence, "shared_tunnel_snapshot", return_value=shared))
                stack.enter_context(mock.patch.object(fence, "port_rows", return_value=[]))
                stack.enter_context(mock.patch.object(fence, "validate_drama_checkpoint"))
                stack.enter_context(mock.patch.object(
                    fence, "backup_unit_definition", side_effect=backup))
                stack.enter_context(mock.patch.object(
                    fence, "verified_definition_backup", return_value=absent_capability()))
                stack.enter_context(mock.patch.object(
                    fence, "validate_recorded_definition_current"))
                stack.enter_context(mock.patch.object(
                    fence, "current_systemd_unit_paths",
                    return_value=definition["fragment"]["systemd_unit_paths"]))
                stack.enter_context(mock.patch.object(
                    fence, "drama_wants_link_record",
                    side_effect=lambda fragment, unit_paths, allow_absent=False: {
                        "kind": "dangling-symlink" if runtime["wants"] else "absent"}))
                stack.enter_context(mock.patch.object(
                    fence, "path_lexists",
                    side_effect=lambda path: runtime["masked"]
                    if path == fence.DRAMA_LOCAL_FRAGMENT else False))
                stack.enter_context(mock.patch.object(
                    fence, "is_persistent_mask", side_effect=lambda path: runtime["masked"]))
                stack.enter_context(mock.patch.object(
                    fence, "drama_fragment_symlink_records", return_value=[]))
                stack.enter_context(mock.patch.object(
                    fence, "original_process_identity_is_gone", return_value=True))
                retire = stack.enter_context(mock.patch.object(fence, "retire_local_unit"))
                success = stack.enter_context(mock.patch.object(
                    fence, "write_drama_success_evidence"))
                stack.enter_context(mock.patch.object(fence, "prop", side_effect=prop))
                run = stack.enter_context(mock.patch.object(fence, "run", side_effect=command))
                fence.main()
        retire.assert_not_called()
        success.assert_called_once()
        self.assertEqual([call[0][0] for call in run.call_args_list], [
            ["systemctl", "stop", fence.DRAMA_UNIT],
            ["systemctl", "disable", "--no-reload", fence.DRAMA_UNIT],
            ["systemctl", "mask", "--no-reload", fence.DRAMA_UNIT],
            ["systemctl", "daemon-reload"],
            ["systemctl", "stop", fence.DRAMA_UNIT],
        ])

    def test_loaded_absent_resume_accepts_stopped_and_not_found_but_rejects_drift(self):
        definition = loaded_absent_definition()
        loaded = definition["fragment"]["loaded_unit"]
        stopped = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive",
                   "substate": "dead", "enabled": "disabled", "control_pid": 0,
                   "control_group": "", "fragment": loaded["fragment_path"],
                   "load_state": "loaded", "id": fence.DRAMA_UNIT,
                   "names": [fence.DRAMA_UNIT],
                   "start_monotonic": loaded["exec_main_start_monotonic"],
                   "active_enter_monotonic": loaded["active_enter_monotonic"],
                   "nrestarts": loaded["nrestarts"]}
        with mock.patch.object(fence, "path_lexists", return_value=False), \
             mock.patch.object(fence, "prop", return_value=""), \
             mock.patch.object(fence, "current_systemd_unit_paths",
                               return_value=definition["fragment"]["systemd_unit_paths"]), \
             mock.patch.object(fence, "drama_wants_link_record",
                               return_value={"path": str(fence.DRAMA_WANTS_LINK),
                                             "kind": "absent"}):
            fence.validate_recorded_definition_current(definition, stopped, resume=True)
            not_found = dict(stopped, load_state="not-found", fragment="")
            fence.validate_recorded_definition_current(definition, not_found, resume=True)
            with self.assertRaisesRegex(RuntimeError, "unexpected not-found"):
                fence.validate_recorded_definition_current(definition, not_found, resume=False)
            with mock.patch.object(fence, "drama_wants_link_record",
                                   return_value={"kind": "dangling-symlink",
                                                 "path": "/unexpected"}):
                with self.assertRaisesRegex(RuntimeError, "enablement link identity changed"):
                    fence.validate_recorded_definition_current(definition, stopped, resume=True)
        with mock.patch.object(fence, "path_lexists",
                               side_effect=lambda path: path == fence.DRAMA_DROPIN_DIR):
            with self.assertRaisesRegex(RuntimeError, "drop-in appeared"):
                fence.validate_recorded_definition_current(definition, stopped, resume=True)

    def test_source_main_drama_stage_failure_invokes_audited_fail_closed(self):
        shared = {"active": "active", "substate": "running", "pid": 456,
                  "control_pid": 0, "control_group": "/tunnel", "start_monotonic": "1",
                  "active_enter_monotonic": "2"}
        definition = {"fragment": {"path": "/etc/systemd/system/drama-material-api.service",
                                     "kind": "file", "content_sha256": "b" * 64},
                      "dropins": [], "definition_sha256": "c" * 64}
        state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active", "substate": "running",
                 "enabled": "enabled", "control_pid": 0, "control_group": "/drama",
                 "fragment": definition["fragment"]["path"], "threads": 1, "children": [],
                 "definition": definition}
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder) / "source-fence"
            base.mkdir()
            checkpoint = pathlib.Path(folder) / "checkpoint.json"
            checkpoint.write_text(json.dumps({"group": "drama", "run_id": fence.RUN_ID,
                                              "checked_at_epoch": __import__("time").time(),
                                              "new_admission_closed": True, "triggers_paused": True,
                                              "cpu_drained": True, "no_unknown": True}))

            def backup(row, unit_backup, resume=False):
                (unit_backup / "original.service").write_text("[Service]\n")
                fence.write_private_json(unit_backup / "definition.json", row["definition"])

            def command(args, check=True):
                if args[:2] == ["systemctl", "mask"]:
                    raise RuntimeError("fixture mask failure")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(sys, "argv", ["source_fence.py", "drama", "--checkpoint",
                                                  str(checkpoint), "--apply"]), \
                 mock.patch.object(fence.socket, "gethostname", return_value="VM-0-13-centos"), \
                 mock.patch.object(fence, "BASE", base), \
                 mock.patch.object(fence, "source_storage_guard"), \
                 mock.patch.object(fence, "inspect", return_value=state), \
                 mock.patch.object(fence, "unit_definition_snapshot", return_value=definition), \
                 mock.patch.object(fence, "shared_tunnel_snapshot", return_value=shared), \
                 mock.patch.object(fence, "validate_shared_tunnel_baseline"), \
                 mock.patch.object(fence, "port_rows", return_value=[]), \
                 mock.patch.object(fence, "validate_drama_checkpoint"), \
                 mock.patch.object(fence, "backup_unit_definition", side_effect=backup), \
                 mock.patch.object(fence, "verified_definition_backup", return_value=archived_capability()), \
                 mock.patch.object(fence, "validate_recorded_definition_current"), \
                 mock.patch.object(fence, "closure_state",
                                   return_value=({"active": "inactive"}, True, None)), \
                 mock.patch.object(fence, "prop", side_effect=lambda unit, name: {
                     "UnitFileState": "enabled", "MainPID": "0", "ActiveState": "inactive"}[name]), \
                 mock.patch.object(fence, "run", side_effect=command), \
                 mock.patch.object(fence, "fail_closed_drama") as fail_closed:
                with self.assertRaisesRegex(RuntimeError, "fixture mask failure"):
                    fence.main()
        self.assertEqual(fail_closed.call_args[0][0], "mask-source")
        self.assertIsInstance(fail_closed.call_args[0][2], RuntimeError)
        self.assertEqual(fail_closed.call_args[0][3].name, fence.DRAMA_UNIT)

    def test_source_main_drama_each_mutation_stage_enters_fail_closed(self):
        shared = complete_shared_tunnel()
        definition = {"fragment": {"path": str(fence.DRAMA_LOCAL_FRAGMENT),
                                     "kind": "file", "content_sha256": "b" * 64},
                      "dropins": [], "definition_sha256": "c" * 64}
        cases = [
            ("first-stop", "stop-source"),
            ("disable", "disable-source"),
            ("mask", "mask-source"),
            ("reload", "daemon-reload"),
            ("second-stop", "post-reload-stop"),
        ]
        for failure, expected_stage in cases:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                base = pathlib.Path(folder) / "source-fence"
                base.mkdir()
                checkpoint = pathlib.Path(folder) / "checkpoint.json"
                checkpoint.write_text(json.dumps({"group": "drama", "run_id": fence.RUN_ID,
                                                  "checked_at_epoch": __import__("time").time(),
                                                  "new_admission_closed": True,
                                                  "triggers_paused": True, "cpu_drained": True,
                                                  "no_unknown": True}))
                state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                         "substate": "running", "enabled": "enabled", "control_pid": 0,
                         "control_group": "/drama", "fragment": str(fence.DRAMA_LOCAL_FRAGMENT),
                         "threads": 1, "children": []}
                stop_count = [0]
                runtime = {"masked": False}

                def command(args, check=True):
                    key = None
                    if args[:2] == ["systemctl", "stop"]:
                        stop_count[0] += 1
                        key = "first-stop" if stop_count[0] == 1 else "second-stop"
                    elif args[:2] == ["systemctl", "disable"]:
                        key = "disable"
                    elif args[:2] == ["systemctl", "mask"]:
                        key = "mask"
                    elif args == ["systemctl", "daemon-reload"]:
                        key = "reload"
                    if key == failure:
                        raise RuntimeError("fixture " + failure)
                    if key == "mask":
                        runtime["masked"] = True
                    return mock.Mock(returncode=0, stdout="", stderr="")

                def backup(row, unit_backup, resume=False):
                    (unit_backup / "original.service").write_text("[Service]\n")
                    fence.write_private_json(unit_backup / "definition.json", row["definition"])

                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(
                        sys, "argv", ["source_fence.py", "drama", "--checkpoint",
                                      str(checkpoint), "--apply"]))
                    stack.enter_context(mock.patch.object(
                        fence.socket, "gethostname", return_value="VM-0-13-centos"))
                    stack.enter_context(mock.patch.object(fence, "BASE", base))
                    stack.enter_context(mock.patch.object(fence, "source_storage_guard"))
                    stack.enter_context(mock.patch.object(fence, "inspect", return_value=state))
                    stack.enter_context(mock.patch.object(
                        fence, "unit_definition_snapshot", return_value=definition))
                    stack.enter_context(mock.patch.object(
                        fence, "shared_tunnel_snapshot", return_value=shared))
                    stack.enter_context(mock.patch.object(fence, "validate_shared_tunnel_baseline"))
                    stack.enter_context(mock.patch.object(fence, "port_rows", return_value=[]))
                    stack.enter_context(mock.patch.object(fence, "validate_drama_checkpoint"))
                    stack.enter_context(mock.patch.object(
                        fence, "backup_unit_definition", side_effect=backup))
                    stack.enter_context(mock.patch.object(
                        fence, "verified_definition_backup", return_value=archived_capability()))
                    stack.enter_context(mock.patch.object(
                        fence, "validate_recorded_definition_current"))
                    stack.enter_context(mock.patch.object(
                        fence, "closure_state", return_value=({"active": "inactive"}, True, None)))
                    stack.enter_context(mock.patch.object(
                        fence, "is_persistent_mask", return_value=True))
                    stack.enter_context(mock.patch.object(
                        fence, "prop", side_effect=lambda unit, name: {
                            "UnitFileState": "masked" if runtime["masked"] else "enabled",
                            "MainPID": "0", "ActiveState": "inactive",
                            "LoadState": "masked" if runtime["masked"] else "loaded"}[name]))
                    stack.enter_context(mock.patch.object(fence, "run", side_effect=command))
                    fail_closed = stack.enter_context(mock.patch.object(fence, "fail_closed_drama"))
                    with self.assertRaisesRegex(RuntimeError, "fixture " + failure):
                        fence.main()
                self.assertEqual(fail_closed.call_args[0][0], expected_stage)

    def test_source_main_drama_resume_accepts_verified_already_masked_only(self):
        shared = complete_shared_tunnel()
        definition = {"fragment": {"path": "/etc/systemd/system/drama-material-api.service",
                                     "kind": "file", "content_sha256": "b" * 64},
                      "dropins": [], "definition_sha256": "c" * 64}
        initial_state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                         "substate": "running", "enabled": "enabled", "control_pid": 0,
                         "control_group": "/drama", "fragment": definition["fragment"]["path"],
                         "threads": 1, "children": [], "definition": definition}
        final_state = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive",
                       "substate": "dead", "enabled": "masked", "load_state": "masked", "control_pid": 0,
                       "control_group": "", "cgroup_pids": [], "fragment": ""}
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder) / "source-fence"
            base.mkdir()
            (base / "drama-before.json").write_text(json.dumps({"states": [initial_state],
                                                                 "shared_tunnel": shared,
                                                                 "port_8787_established": []}))
            unit_backup = base / fence.DRAMA_UNIT
            unit_backup.mkdir(mode=0o700)
            fence.write_private_json(unit_backup / "definition.json", definition)
            checkpoint = pathlib.Path(folder) / "checkpoint.json"
            checkpoint.write_text(json.dumps({"group": "drama", "run_id": fence.RUN_ID,
                                              "checked_at_epoch": __import__("time").time(),
                                              "new_admission_closed": True, "triggers_paused": True,
                                              "cpu_drained": True, "no_unknown": True}))
            with mock.patch.object(sys, "argv", ["source_fence.py", "drama", "--checkpoint",
                                                  str(checkpoint), "--apply", "--resume"]), \
                 mock.patch.object(fence.socket, "gethostname", return_value="VM-0-13-centos"), \
                 mock.patch.object(fence, "BASE", base), \
                 mock.patch.object(fence, "source_storage_guard"), \
                 mock.patch.object(fence, "inspect", return_value=final_state), \
                 mock.patch.object(fence, "shared_tunnel_snapshot", return_value=shared), \
                 mock.patch.object(fence, "port_rows", return_value=[]), \
                 mock.patch.object(fence, "validate_drama_checkpoint"), \
                 mock.patch.object(fence, "verified_definition_backup", return_value=archived_capability()), \
                 mock.patch.object(fence, "is_persistent_mask", return_value=True), \
                 mock.patch.object(fence, "prop", side_effect=lambda unit, name: {
                     "UnitFileState": "masked", "MainPID": "0", "ActiveState": "inactive"}[name]), \
                 mock.patch.object(fence, "run") as run, \
                 mock.patch.object(fence, "write_drama_success_evidence") as success:
                fence.main()
        run.assert_not_called()
        success.assert_called_once()

    def test_source_main_drama_resume_rejects_unverified_already_masked_backup(self):
        shared = complete_shared_tunnel()
        definition = {"fragment": {"path": "/etc/systemd/system/drama-material-api.service",
                                     "kind": "file", "content_sha256": "b" * 64},
                      "dropins": [], "definition_sha256": "c" * 64}
        initial_state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                         "substate": "running", "enabled": "enabled", "control_pid": 0,
                         "control_group": "/drama", "fragment": definition["fragment"]["path"],
                         "threads": 1, "children": [], "definition": definition}
        final_state = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive",
                       "substate": "dead", "enabled": "masked", "load_state": "masked", "control_pid": 0,
                       "control_group": "", "cgroup_pids": [], "fragment": ""}
        for backup_error in ("MissingBackup", "CorruptBackup", "SymlinkBackup"):
            with tempfile.TemporaryDirectory() as folder:
                base = pathlib.Path(folder) / "source-fence"
                base.mkdir()
                (base / "drama-before.json").write_text(json.dumps({"states": [initial_state],
                                                                     "shared_tunnel": shared,
                                                                     "port_8787_established": []}))
                checkpoint = pathlib.Path(folder) / "checkpoint.json"
                checkpoint.write_text(json.dumps({"group": "drama", "run_id": fence.RUN_ID,
                                                  "checked_at_epoch": __import__("time").time(),
                                                  "new_admission_closed": True,
                                                  "triggers_paused": True, "cpu_drained": True,
                                                  "no_unknown": True}))
                with mock.patch.object(sys, "argv", ["source_fence.py", "drama", "--checkpoint",
                                                      str(checkpoint), "--apply", "--resume"]), \
                     mock.patch.object(fence.socket, "gethostname",
                                       return_value="VM-0-13-centos"), \
                     mock.patch.object(fence, "BASE", base), \
                     mock.patch.object(fence, "source_storage_guard"), \
                     mock.patch.object(fence, "inspect", return_value=final_state), \
                     mock.patch.object(fence, "shared_tunnel_snapshot", return_value=shared), \
                     mock.patch.object(fence, "port_rows", return_value=[]), \
                     mock.patch.object(fence, "validate_drama_checkpoint"), \
                     mock.patch.object(fence, "verified_definition_backup",
                                       return_value=failed_capability(backup_error)), \
                     mock.patch.object(fence, "prop", side_effect=lambda unit, name: {
                         "UnitFileState": "masked", "MainPID": "0",
                         "ActiveState": "inactive"}[name]), \
                     mock.patch.object(fence, "run") as run, \
                     mock.patch.object(fence, "write_drama_success_evidence") as success:
                    with self.assertRaisesRegex(RuntimeError, "definition backup is not verified"):
                        fence.main()
                run.assert_not_called()
                success.assert_not_called()

    def test_already_masked_resume_binds_manifest_to_initial_snapshot(self):
        shared = complete_shared_tunnel()
        definition = loaded_absent_definition()
        changed = json.loads(json.dumps(definition))
        changed["definition_sha256"] = "f" * 64
        initial_state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                         "substate": "running", "enabled": "enabled", "control_pid": 0,
                         "control_group": definition["fragment"]["loaded_unit"]["control_group"],
                         "fragment": str(fence.DRAMA_LOCAL_FRAGMENT), "threads": 1,
                         "children": [], "definition": definition}
        live_state = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive",
                      "substate": "dead", "enabled": "masked", "load_state": "masked",
                      "control_pid": 0, "control_group": "", "cgroup_pids": [], "fragment": ""}
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder) / "source-fence"
            base.mkdir()
            fence.write_private_json(base / "drama-before.json", {
                "states": [initial_state], "shared_tunnel": shared,
                "port_8787_established": []})
            unit_backup = base / fence.DRAMA_UNIT
            unit_backup.mkdir(mode=0o700)
            fence.write_private_json(unit_backup / "definition.json", changed)
            args = mock.Mock(group="drama", resume=True)
            with mock.patch.object(fence, "BASE", base), \
                 mock.patch.object(fence, "prop", side_effect=lambda unit, name: {
                     "UnitFileState": "masked", "MainPID": "0",
                     "ActiveState": "inactive"}[name]), \
                 mock.patch.object(fence, "verified_definition_backup",
                                   return_value=absent_capability()), \
                 mock.patch.object(fence, "validate_drama_fenced") as validate:
                with self.assertRaisesRegex(RuntimeError, "differs from initial snapshot"):
                    fence.apply_locked_source_fence(
                        args, [live_state], shared, "a" * 64)
            validate.assert_not_called()

    def test_source_main_drama_resume_rejects_stopped_unmasked_definition_drift(self):
        shared = complete_shared_tunnel()
        original_definition = {"fragment": {"path": "/etc/systemd/system/drama-material-api.service",
                                              "kind": "file", "content_sha256": "b" * 64},
                               "dropins": [], "definition_sha256": "c" * 64}
        changed_definition = dict(original_definition, definition_sha256="d" * 64)
        initial_state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                         "substate": "running", "enabled": "enabled", "control_pid": 0,
                         "control_group": "/drama", "fragment": original_definition["fragment"]["path"],
                         "threads": 1, "children": [], "definition": original_definition}
        live_state = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive",
                      "substate": "dead", "enabled": "disabled", "control_pid": 0,
                      "control_group": "", "fragment": original_definition["fragment"]["path"]}
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder) / "source-fence"
            base.mkdir()
            (base / "drama-before.json").write_text(json.dumps({"states": [initial_state],
                                                                  "shared_tunnel": shared,
                                                                  "port_8787_established": []}))
            checkpoint = pathlib.Path(folder) / "checkpoint.json"
            checkpoint.write_text(json.dumps({"group": "drama", "run_id": fence.RUN_ID,
                                              "checked_at_epoch": __import__("time").time(),
                                              "new_admission_closed": True, "triggers_paused": True,
                                              "cpu_drained": True, "no_unknown": True}))
            with mock.patch.object(sys, "argv", ["source_fence.py", "drama", "--checkpoint",
                                                  str(checkpoint), "--apply", "--resume"]), \
                 mock.patch.object(fence.socket, "gethostname", return_value="VM-0-13-centos"), \
                 mock.patch.object(fence, "BASE", base), \
                 mock.patch.object(fence, "source_storage_guard"), \
                 mock.patch.object(fence, "inspect", return_value=live_state), \
                 mock.patch.object(fence, "unit_definition_snapshot", return_value=changed_definition), \
                 mock.patch.object(fence, "shared_tunnel_snapshot", return_value=shared), \
                 mock.patch.object(fence, "port_rows", return_value=[]), \
                 mock.patch.object(fence, "validate_drama_checkpoint"), \
                 mock.patch.object(fence, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "changed from its recorded identity"):
                    fence.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
