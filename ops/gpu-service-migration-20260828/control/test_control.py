import importlib.util
import base64
import contextlib
import hashlib
import io
import json
import pathlib
import re
import sqlite3
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
                      "POST:/api/ad-material-other/jobs"]:
            self.assertFalse(any(p.search(route) for p in patterns), route)

    def test_group_gate_retains_other_groups(self):
        text, gate = maintenance.gate_text({"tt", "materials"})
        self.assertIn("tt-posts", text)
        self.assertIn("drama-screenshot-material", text)
        self.assertNotIn("x-posts", text)
        self.assertIn("503", gate)
        self.assertIn('~^[A-Z]+:/api/drama-screenshot-material/jobs/batch$', text)

    def test_no_main_api_oauth_or_metrics_trigger_stop(self):
        units = maintenance.TRIGGERS["tt"] + maintenance.TRIGGERS["x"]
        self.assertTrue(all(u.endswith((".timer", ".path")) for u in units))
        self.assertFalse(any("metric" in u for u in units))

    def test_source_scope_and_idle_guard(self):
        units = sum(fence.GROUPS.values(), [])
        self.assertEqual(len(units), len(set(units)))
        self.assertEqual(len([u for u in units if "tunnel" not in u]), 12)
        self.assertFalse(any("kronos" in u or "fb-page" in u for u in units))
        self.assertEqual(fence.GROUPS["drama"], ["drama-material-api.service"])
        self.assertNotIn("drama-material-api.service", fence.GROUPS["materials"])
        self.assertIn("gpu-worker-reverse-tunnel.service", fence.GROUPS["materials"])
        idle = {"unit": "tt-gpu-publisher.service", "pid": 123, "threads": 1, "children": []}
        fence.assert_idle([idle])
        with self.assertRaises(RuntimeError):
            fence.assert_idle([dict(idle, threads=2)])
        with self.assertRaises(RuntimeError):
            fence.assert_idle([dict(idle, children=["234"])])

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
            backup.mkdir()
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
        shared = {"unit": fence.DRAMA_SHARED_TUNNEL, "active": "active", "substate": "running",
                  "enabled": "enabled", "pid": 456, "control_pid": 0,
                  "control_group": "/system.slice/gpu-worker-reverse-tunnel.service",
                  "nrestarts": 0, "start_monotonic": "100", "active_enter_monotonic": "101",
                  "unit_sha256": "a" * 64}
        with mock.patch.object(fence, "port_rows", return_value=[]):
            fence.validate_drama_preflight([source])
        fence.validate_shared_tunnel_baseline(shared)
        with mock.patch.object(fence, "port_rows", return_value=[]), \
             mock.patch.object(fence, "inspect", return_value={
                 "unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive", "substate": "dead",
                 "enabled": "masked", "control_pid": 0, "control_group": ""}), \
             mock.patch.object(fence, "shared_tunnel_snapshot", return_value=shared):
            _, after = fence.validate_drama_fenced(shared)
        self.assertEqual(after, shared)
        with mock.patch.object(fence, "port_rows", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "single-threaded"):
                fence.validate_drama_preflight([dict(source, threads=2)])

    def test_drama_source_fence_detects_shared_tunnel_restart(self):
        before = {"pid": 456, "nrestarts": 0, "unit_sha256": "a" * 64}
        after = dict(before, pid=789, nrestarts=1)
        final = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive", "substate": "dead",
                 "enabled": "masked", "control_pid": 0, "control_group": ""}
        with mock.patch.object(fence, "port_rows", return_value=[]), \
             mock.patch.object(fence, "inspect", return_value=final), \
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
            backup.mkdir()
            state = {"unit": fence.DRAMA_UNIT, "definition": definition,
                     "fragment": str(fragment)}
            with mock.patch.object(fence, "unit_definition_snapshot", return_value=definition):
                fence.backup_unit_definition(state, backup)
            self.assertEqual((backup / "original.service").read_bytes(), fragment.read_bytes())
            self.assertEqual((backup / "dropins/000.service").read_bytes(), target.read_bytes())
            self.assertEqual(json.loads((backup / "definition.json").read_text()), definition)
            self.assertEqual(fence.verified_definition_backup(backup), (True, None))
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
            ok, error = fence.verified_definition_backup(root)
            self.assertFalse(ok)
            self.assertEqual(error, "RuntimeError")

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

    def test_drama_source_failure_closes_only_retired_api(self):
        original = ValueError("fixture failure")
        with tempfile.TemporaryDirectory() as folder:
            backup = pathlib.Path(folder)
            (backup / "original.service").write_text("[Service]\n")
            with mock.patch.object(fence, "best_effort_command",
                                   return_value={"rc": 0, "error_type": None}) as command, \
                 mock.patch.object(fence, "verified_definition_backup", return_value=(True, None)), \
                 mock.patch.object(fence, "closure_state", return_value=({"active": "inactive"}, True, None)), \
                 mock.patch.object(fence, "write_failure_evidence") as evidence:
                fence.fail_closed_drama("mask-source", {"pid": 456}, original, backup)
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            ["systemctl", "stop", "drama-material-api.service"],
            ["systemctl", "disable", "drama-material-api.service"],
            ["systemctl", "mask", "drama-material-api.service"],
            ["systemctl", "daemon-reload"],
        ])
        self.assertFalse(any(fence.DRAMA_SHARED_TUNNEL in str(call) for call in command.call_args_list))
        call = evidence.call_args
        self.assertEqual(call.args[0:2], ("mask-source", "ValueError"))
        self.assertTrue(call.args[4])

    def test_drama_source_failure_raises_high_risk_when_closure_unproven(self):
        original = ValueError("fixture failure")
        with tempfile.TemporaryDirectory() as folder:
            backup = pathlib.Path(folder)
            (backup / "original.service").write_text("[Service]\n")
            with mock.patch.object(fence, "best_effort_command",
                                   return_value={"rc": 1, "error_type": None}), \
                 mock.patch.object(fence, "verified_definition_backup", return_value=(True, None)), \
                 mock.patch.object(fence, "closure_state",
                                   return_value=({"active": "active", "pid": 99}, False, None)), \
                 mock.patch.object(fence, "write_failure_evidence") as evidence:
                with self.assertRaisesRegex(RuntimeError, "HIGH RISK") as raised:
                    fence.fail_closed_drama("stop-source", {"pid": 456}, original, backup)
        self.assertIs(raised.exception.__cause__, original)
        self.assertFalse(evidence.call_args.args[4])
        commands = evidence.call_args.args[2]
        self.assertEqual(commands["stop"]["rc"], 1)
        self.assertEqual(commands["disable"]["rc"], 1)

    def test_drama_source_failure_evidence_error_preserves_original_cause(self):
        for final_closed in (False, True):
            original = ValueError("fixture failure")
            with tempfile.TemporaryDirectory() as folder:
                backup = pathlib.Path(folder)
                with mock.patch.object(fence, "best_effort_command",
                                       return_value={"rc": 0, "error_type": None}), \
                     mock.patch.object(fence, "verified_definition_backup",
                                       return_value=(True, None)), \
                     mock.patch.object(fence, "closure_state",
                                       return_value=({"active": "inactive"}, final_closed, None)), \
                     mock.patch.object(fence, "write_failure_evidence",
                                       side_effect=OSError("evidence fixture failure")):
                    with self.assertRaisesRegex(RuntimeError, "HIGH RISK") as raised:
                        fence.fail_closed_drama("mask-source", {"pid": 456}, original, backup)
            self.assertIs(raised.exception.__cause__, original)

    def test_drama_closure_state_requires_zero_ports_and_successful_probe(self):
        values = {"ActiveState": "inactive", "SubState": "dead",
                  "UnitFileState": "masked", "MainPID": "0", "ControlPID": "0",
                  "ControlGroup": ""}
        with mock.patch.object(fence, "prop", side_effect=lambda unit, name: values[name]), \
             mock.patch.object(fence, "port_rows", return_value=[]):
            state, closed, error = fence.closure_state(require_masked=True)
        self.assertTrue(closed)
        self.assertIsNone(error)
        self.assertEqual(state["port_8787_listener_count"], 0)
        self.assertEqual(state["port_8787_established_count"], 0)

        with mock.patch.object(fence, "prop", side_effect=lambda unit, name: values[name]), \
             mock.patch.object(fence, "port_rows", side_effect=[["LISTEN 8787"], []]):
            state, closed, error = fence.closure_state(require_masked=True)
        self.assertFalse(closed)
        self.assertIsNone(error)
        self.assertEqual(state["port_8787_listener_count"], 1)

        with mock.patch.object(fence, "prop", side_effect=lambda unit, name: values[name]), \
             mock.patch.object(fence, "port_rows", side_effect=[[], ["ESTAB 8787"]]):
            state, closed, error = fence.closure_state(require_masked=True)
        self.assertFalse(closed)
        self.assertIsNone(error)
        self.assertEqual(state["port_8787_established_count"], 1)

        with mock.patch.object(fence, "prop", side_effect=lambda unit, name: values[name]), \
             mock.patch.object(fence, "port_rows", side_effect=RuntimeError("ss failed")):
            _state, closed, error = fence.closure_state(require_masked=True)
        self.assertFalse(closed)
        self.assertEqual(error, "RuntimeError")

    def test_source_main_drama_success_stops_masks_and_verifies_final_state(self):
        shared = {"unit": fence.DRAMA_SHARED_TUNNEL, "active": "active", "substate": "running",
                  "enabled": "enabled", "pid": 456, "control_pid": 0,
                  "control_group": "/system.slice/gpu-worker-reverse-tunnel.service",
                  "nrestarts": 0, "start_monotonic": "100", "active_enter_monotonic": "101",
                  "unit_sha256": "a" * 64}
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
                    "enabled": runtime["enabled"], "control_pid": 0, "control_group": "",
                    "fragment": ""}

        def prop(unit, name):
            values = {"UnitFileState": runtime["enabled"], "MainPID": "123" if runtime["active"] else "0",
                      "ActiveState": "active" if runtime["active"] else "inactive"}
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
                 mock.patch.object(fence, "verified_definition_backup", return_value=(True, None)), \
                 mock.patch.object(fence, "write_drama_success_evidence") as success, \
                 mock.patch.object(fence, "prop", side_effect=prop), \
                 mock.patch.object(fence, "run", side_effect=command) as run:
                fence.main()
        self.assertEqual(runtime, {"active": False, "enabled": "masked"})
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands, [["systemctl", "stop", fence.DRAMA_UNIT],
                                    ["systemctl", "disable", fence.DRAMA_UNIT],
                                    ["systemctl", "mask", fence.DRAMA_UNIT],
                                    ["systemctl", "daemon-reload"]])
        success.assert_called_once()

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
                 mock.patch.object(fence, "verified_definition_backup", return_value=(True, None)), \
                 mock.patch.object(fence, "prop", side_effect=lambda unit, name: {
                     "UnitFileState": "enabled", "MainPID": "0", "ActiveState": "inactive"}[name]), \
                 mock.patch.object(fence, "run", side_effect=command), \
                 mock.patch.object(fence, "fail_closed_drama") as fail_closed:
                with self.assertRaisesRegex(RuntimeError, "fixture mask failure"):
                    fence.main()
        self.assertEqual(fail_closed.call_args.args[0], "mask-source")
        self.assertIsInstance(fail_closed.call_args.args[2], RuntimeError)
        self.assertEqual(fail_closed.call_args.args[3].name, fence.DRAMA_UNIT)

    def test_source_main_drama_resume_accepts_verified_already_masked_only(self):
        shared = {"unit": fence.DRAMA_SHARED_TUNNEL, "active": "active", "substate": "running",
                  "enabled": "enabled", "pid": 456, "control_pid": 0, "control_group": "/tunnel",
                  "nrestarts": 0, "start_monotonic": "1", "active_enter_monotonic": "2",
                  "unit_sha256": "a" * 64}
        definition = {"fragment": {"path": "/etc/systemd/system/drama-material-api.service",
                                     "kind": "file", "content_sha256": "b" * 64},
                      "dropins": [], "definition_sha256": "c" * 64}
        initial_state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                         "substate": "running", "enabled": "enabled", "control_pid": 0,
                         "control_group": "/drama", "fragment": definition["fragment"]["path"],
                         "threads": 1, "children": [], "definition": definition}
        final_state = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive",
                       "substate": "dead", "enabled": "masked", "control_pid": 0,
                       "control_group": "", "fragment": ""}
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
                 mock.patch.object(fence, "inspect", return_value=final_state), \
                 mock.patch.object(fence, "shared_tunnel_snapshot", return_value=shared), \
                 mock.patch.object(fence, "port_rows", return_value=[]), \
                 mock.patch.object(fence, "validate_drama_checkpoint"), \
                 mock.patch.object(fence, "verified_definition_backup", return_value=(True, None)), \
                 mock.patch.object(fence, "prop", side_effect=lambda unit, name: {
                     "UnitFileState": "masked", "MainPID": "0", "ActiveState": "inactive"}[name]), \
                 mock.patch.object(fence, "run") as run, \
                 mock.patch.object(fence, "write_drama_success_evidence") as success:
                fence.main()
        run.assert_not_called()
        success.assert_called_once()

    def test_source_main_drama_resume_rejects_unverified_already_masked_backup(self):
        shared = {"unit": fence.DRAMA_SHARED_TUNNEL, "active": "active", "substate": "running",
                  "enabled": "enabled", "pid": 456, "control_pid": 0, "control_group": "/tunnel",
                  "nrestarts": 0, "start_monotonic": "1", "active_enter_monotonic": "2",
                  "unit_sha256": "a" * 64}
        definition = {"fragment": {"path": "/etc/systemd/system/drama-material-api.service",
                                     "kind": "file", "content_sha256": "b" * 64},
                      "dropins": [], "definition_sha256": "c" * 64}
        initial_state = {"unit": fence.DRAMA_UNIT, "pid": 123, "active": "active",
                         "substate": "running", "enabled": "enabled", "control_pid": 0,
                         "control_group": "/drama", "fragment": definition["fragment"]["path"],
                         "threads": 1, "children": [], "definition": definition}
        final_state = {"unit": fence.DRAMA_UNIT, "pid": 0, "active": "inactive",
                       "substate": "dead", "enabled": "masked", "control_pid": 0,
                       "control_group": "", "fragment": ""}
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
                                       return_value=(False, backup_error)), \
                     mock.patch.object(fence, "prop", side_effect=lambda unit, name: {
                         "UnitFileState": "masked", "MainPID": "0",
                         "ActiveState": "inactive"}[name]), \
                     mock.patch.object(fence, "run") as run, \
                     mock.patch.object(fence, "write_drama_success_evidence") as success:
                    with self.assertRaisesRegex(RuntimeError, "definition backup is not verified"):
                        fence.main()
                run.assert_not_called()
                success.assert_not_called()

    def test_source_main_drama_resume_rejects_stopped_unmasked_definition_drift(self):
        shared = {"unit": fence.DRAMA_SHARED_TUNNEL, "active": "active", "substate": "running",
                  "enabled": "enabled", "pid": 456, "control_pid": 0, "control_group": "/tunnel",
                  "nrestarts": 0, "start_monotonic": "1", "active_enter_monotonic": "2",
                  "unit_sha256": "a" * 64}
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
                with self.assertRaisesRegex(RuntimeError, "changed since initial fence snapshot"):
                    fence.main()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
