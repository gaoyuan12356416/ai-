import importlib.util
import pathlib
import re
import unittest
import sys

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

    def test_no_main_api_oauth_or_metrics_trigger_stop(self):
        units = maintenance.TRIGGERS["tt"] + maintenance.TRIGGERS["x"]
        self.assertTrue(all(u.endswith((".timer", ".path")) for u in units))
        self.assertFalse(any("metric" in u for u in units))

    def test_source_scope_and_idle_guard(self):
        units = sum(fence.GROUPS.values(), [])
        self.assertEqual(len(units), len(set(units)))
        self.assertEqual(len([u for u in units if "tunnel" not in u]), 12)
        self.assertFalse(any("kronos" in u or "fb-page" in u for u in units))
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


if __name__ == "__main__":
    unittest.main()
