"""Random-template schedules reject known images before creating any queue."""
import copy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import x_post_schedule_runner as runner
from scripts import test_x_post_schedule_runner as schedule_fixture
from scripts import test_x_post_material_pool_selector as pool_fixture


class Connection(pool_fixture.PoolConnection):
    def __init__(self):
        super().__init__([30, 20, 10])
        self.materials["30"].update(material_type=1, video_duration=0,
                                    material_url="https://media.example.test/30.png")
        for material_id in (30, 20):
            self.drama_rows[str(material_id)] = [pool_fixture.drama_row(material_id, language="ja")]
            self.deploy_rows[str(material_id)] = [pool_fixture.deploy_row(material_id, language="ja")]
        self.closed = False

    def close(self):
        self.closed = True


class Sidecar(schedule_fixture.FakeSidecar):
    def __init__(self, random_enabled, *, existing_queues=False):
        due = schedule_fixture.due_item(accounts=[11, 12], frozen=True)
        # A current/due value cannot replace the original frozen setting.
        due["random_template_enabled"] = not random_enabled
        super().__init__([due])
        self.checks = []
        self.pool_reads = 0
        self.existing[("material", "2026-07-27", "10:00", 3)] = {
            "found": True,
            "run": {**schedule_fixture.frozen_run("material", 2),
                    "status": "claimed", "random_template_enabled": random_enabled},
            "queues": ([schedule_fixture.queue(91, 11, 1), schedule_fixture.queue(92, 12, 2)]
                       if existing_queues else []),
        }

    def available_pool_items(self, _path, _limit, *, schedule_identity=None):
        self.pool_reads += 1
        return [pool_fixture.pool_item(i, i, "2026-07-27T00:00:%02dZ" % i)
                for i in (30, 20, 10)]

    def record_pool_checks(self, _path, checks):
        self.checks.extend(copy.deepcopy(checks))
        return {"updated_count": len(checks)}

    def verify_account(self, account_id):
        value = super().verify_account(account_id)
        value["drama_language"] = "ja" if account_id == 11 else "en"
        return value


class RandomVideoSelectionTests(unittest.TestCase):
    def execute(self, sidecar, connections):
        forbidden = mock.Mock(side_effect=AssertionError("planner performed media work"))
        original = copy.deepcopy(sidecar.existing)
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(schedule_fixture.make_config(temporary), candidate_pool_limit=1)
            with mock.patch.dict("os.environ", {"PIPELINE_X_DISPATCH_ENABLED": "1"}), \
                    mock.patch.object(runner, "DEFAULT_WORK_DIR", temporary):
                result = runner.execute_schedule_tick(
                    config, sidecar=sidecar,
                    connection_factory=lambda _: connections.pop(0),
                    downloader=forbidden, prober=forbidden, repair_client=forbidden,
                    now=datetime(2026, 7, 27, 10, 0, 30, tzinfo=schedule_fixture.BEIJING),
                )
        forbidden.assert_not_called()
        self.assertEqual(sidecar.existing, original)
        self.assertEqual(result["batches"][0]["status"], "dispatch_queued")
        self.assertFalse(any(call[0] in {"publish", "storage", "failure"} for call in sidecar.calls))
        return [call[2] for call in sidecar.calls if call[0] == "create"]

    def test_frozen_random_template_skips_image_and_continues_pages_for_both_accounts(self):
        sidecar, connection = Sidecar(True), Connection()
        plans = self.execute(sidecar, [connection])
        self.assertEqual(len(plans), 1)
        self.assertEqual([(c["account_id"], c["material_id"]) for c in plans[0]["candidates"]],
                         [(11, "20"), (12, "10")])
        self.assertEqual([(c["pool_item_id"], c["error_code"]) for c in sidecar.checks],
                         [(30, "material_not_video")])
        self.assertEqual(sidecar.pool_reads, 1)
        self.assertTrue(connection.closed)

    def test_frozen_plain_schedule_keeps_image_candidate(self):
        sidecar = Sidecar(False)
        plans = self.execute(sidecar, [Connection()])
        self.assertEqual([(c["account_id"], c["material_id"]) for c in plans[0]["candidates"]],
                         [(11, "30"), (12, "10")])
        self.assertFalse(any(c.get("error_code") == "material_not_video" for c in sidecar.checks))

    def test_read_reconnect_keeps_original_video_only_policy_and_pool_snapshot(self):
        sidecar, failed, recovered = Sidecar(True), Connection(), Connection()
        failed.query_error_material_ids.add("30")
        plans = self.execute(sidecar, [failed, recovered])
        self.assertEqual([c["material_id"] for c in plans[0]["candidates"]], ["20", "10"])
        self.assertEqual([(c["pool_item_id"], c["error_code"]) for c in sidecar.checks],
                         [(30, "material_not_video")])
        self.assertEqual(sidecar.pool_reads, 1)
        self.assertTrue(failed.closed and recovered.closed)

    def test_existing_queues_are_handed_off_without_reselection(self):
        sidecar = Sidecar(True, existing_queues=True)
        self.assertEqual(self.execute(sidecar, []), [])
        self.assertEqual(sidecar.pool_reads, 0)
        self.assertEqual(sidecar.checks, [])


if __name__ == "__main__":
    unittest.main()
