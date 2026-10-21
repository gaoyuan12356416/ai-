"""Independent offline review cases for Excel admission and worker integration."""
import base64
import copy
import io
import sys
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_fb_video_publish_core import Fixture
from scripts import test_fb_excel_publish as excel_fixture
from features.fb_auto_posts.publisher import GraphResult
from features.fb_video_publish.core import DomainError
from features.fb_video_publish.excel import HEADERS, read_workbook
from features.fb_video_publish.service import PREFIX
from features.fb_video_publish.templates import render_template


class IndependentExcelReview(Fixture):
    preview_excel = excel_fixture.ExcelTests.preview_excel
    submit_excel = excel_fixture.ExcelTests.submit_excel

    def setUp(self):
        super().setUp()
        self.sources.pages = excel_fixture.PageMap(self.sources.pages)
        self.sources.resolve_materials = lambda ids, **kwargs: {
            "items": copy.deepcopy({mid: self.sources.materials[mid] for mid in ids if mid in self.sources.materials}),
            "errors": {mid: {"code": "material_not_found", "message": "missing"}
                       for mid in ids if mid not in self.sources.materials},
        }

    def counts(self):
        with self.store.connect() as conn:
            return {name: conn.execute("SELECT COUNT(*) FROM fb_video_" + name).fetchone()[0]
                    for name in ("batch", "task", "link", "attempt")}

    def test_worker_obeys_schedule_then_excel_order_and_receipt_fence(self):
        self.sources.materials["23"] = dict(self.sources.materials["22"], material_id="23")
        self.submit_excel(self.preview_excel([
            excel_fixture.row(mid="21", time="2026-10-22 12:00:00"),
            excel_fixture.row(mid="22", time="2026-10-21 12:00:00"),
            excel_fixture.row(mid="23", time="2026-10-21 12:00:00"),
        ]))
        self.ready()
        worker = self.worker([GraphResult("success", post_id="11_22"),
                              GraphResult("success", post_id="11_23"),
                              GraphResult("success", post_id="11_21")])
        self.assertEqual("no_pending", worker.execute_next()["status"])
        self.now += timedelta(hours=18)
        self.assertEqual("submitted", worker.execute_next()["status"])
        self.assertEqual("22", next(t["material_id"] for t in self.tasks() if t["status"] == "submitted"))
        self.assertEqual("no_pending", worker.execute_next()["status"])
        self.now += timedelta(seconds=61)
        self.assertEqual("published", worker.reconcile_next()["status"])
        self.assertEqual("submitted", worker.execute_next()["status"])
        self.assertEqual("23", next(t["material_id"] for t in self.tasks() if t["status"] == "submitted"))
        self.now += timedelta(seconds=61)
        worker.reconcile_next()
        self.assertEqual("no_pending", worker.execute_next()["status"])
        self.now += timedelta(days=1)
        self.assertEqual("submitted", worker.execute_next()["status"])
        self.assertEqual("21", next(t["material_id"] for t in self.tasks() if t["status"] == "submitted"))
        self.assertEqual(3, len(worker.calls))

    def test_atomic_freeze_rolls_back_links_and_tasks_on_second_row_failure(self):
        preview = self.preview_excel([excel_fixture.row(), excel_fixture.row(mid="22")])
        before, calls = self.counts(), []

        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("offline fault while freezing the second row")
            return render_template(*args, **kwargs)

        with patch("features.fb_video_publish.templates.render_template", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.submit_excel(preview)
        self.assertEqual(2, len(calls))
        self.assertEqual(before, self.counts())
        self.assertIsNone(self.store.existing(self.actor, preview["request"]["operation_id"]))
        result = self.submit_excel(preview)
        self.assertEqual(2, len(result["batch"]["items"]))
        self.assertEqual({"batch": 1, "task": 2, "link": 2, "attempt": 0}, self.counts())

    def test_concurrent_identical_operation_creates_one_batch(self):
        preview = self.preview_excel([excel_fixture.row(), excel_fixture.row(mid="22")])
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.submit_excel(copy.deepcopy(preview)), range(4)))
        self.assertEqual(1, len({result["batch_id"] for result in results}))
        self.assertEqual(1, sum(not result["idempotent"] for result in results))
        self.assertEqual({"batch": 1, "task": 2, "link": 2, "attempt": 0}, self.counts())

    def test_operator_identity_change_requires_new_preview(self):
        preview = self.preview_excel()
        self.actor = {**self.actor, "sub_user_id": "101", "business_owner_id": "101"}
        with self.assertRaises(DomainError) as caught:
            self.submit_excel(preview)
        self.assertEqual("preview_changed", caught.exception.code)
        self.assertEqual(0, self.counts()["batch"])

    def test_new_page_conflict_after_preview_is_atomic(self):
        preview = self.preview_excel([excel_fixture.row(), excel_fixture.row(pid="12", mid="22")])
        self.submit()
        before = self.counts()
        with self.assertRaises(DomainError) as caught:
            self.submit_excel(preview)
        self.assertEqual("fb_video_material_active", caught.exception.code)
        self.assertEqual(before, self.counts())

    def test_history_change_after_preview_invalidates_full_proof(self):
        self.submit()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='cancelled'")
        preview = self.preview_excel([excel_fixture.row(), excel_fixture.row(mid="22")])
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='published',graph_post_id='11_prior'")
        before = self.counts()
        with self.assertRaises(DomainError) as caught:
            self.submit_excel(preview, allow_repeat=False)
        self.assertEqual("preview_changed", caught.exception.code)
        self.assertEqual(before, self.counts())

    def test_excel_shares_unknown_page_fence_with_legacy_tasks(self):
        self.submit()
        result = self.submit_excel(self.preview_excel([excel_fixture.row(mid="22"), excel_fixture.row(pid="12")]))
        self.ready()
        worker = self.worker([GraphResult("unknown"), GraphResult("success", post_id="12_21")])
        self.assertEqual("unknown", worker.execute_next()["status"])
        self.assertEqual("submitted", worker.execute_next()["status"])
        excel_tasks = [task for task in self.tasks() if task["batch_id"] == result["batch_id"]]
        self.assertEqual({"11": "queued", "12": "submitted"},
                         {task["page_id"]: task["status"] for task in excel_tasks})
        self.assertEqual("no_pending", worker.execute_next()["status"])
        self.assertEqual(2, len(worker.calls))

    def test_rollback_ingress_gate_keeps_pending_work_and_mapping_free_reads(self):
        self.service.v2_enabled = False
        result = self.submit_excel(self.preview_excel())
        self.service.excel_enabled = False
        self.ready()
        actor = {**self.actor, "sub_user_id": "", "business_owner_id": ""}
        for suffix in ("/operations/" + result["operation_id"], "/batches/" + result["batch_id"]):
            found = self.service.handle("GET", PREFIX + "/excel" + suffix, actor)
            self.assertEqual(result["batch_id"], found["batch"]["batch_id"])
        worker = self.worker()
        self.assertEqual("submitted", worker.execute_next()["status"])
        self.now += timedelta(seconds=61)
        self.assertEqual("published", worker.reconcile_next()["status"])


class IndependentWorkbookReview(unittest.TestCase):
    def test_optional_trailing_cells_may_be_absent_after_full_column_scan(self):
        content = excel_fixture.xlsx([excel_fixture.row()[:5]])
        _, accepted, skipped, total = read_workbook("fixture.xlsx", content)
        self.assertEqual(1, total)
        self.assertEqual([], skipped)
        self.assertEqual(("", "", None),
                         (accepted[0]["caption_template"], accepted[0]["title"], accepted[0]["scheduled_at_utc"]))

    def test_sparse_styled_rows_cannot_expand_scan_without_bound(self):
        original = io.BytesIO(base64.b64decode(excel_fixture.xlsx([excel_fixture.row()])))
        output = io.BytesIO()
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for member in source.infolist():
                content = source.read(member)
                if member.filename == "xl/worksheets/sheet1.xml":
                    # Only 13 XML cells, but each distant styled cell expands a
                    # row to 16,384 columns in openpyxl's read-only iterator.
                    rows = "".join(f'<row r="{n}"><c r="XFD{n}" s="1"/></row>' for n in range(3, 16))
                    content = content.replace(b"</sheetData>", rows.encode() + b"</sheetData>")
                target.writestr(member.filename, content)
        self.assertLess(len(output.getvalue()), 10_000)
        with self.assertRaises(DomainError) as caught:
            read_workbook("fixture.xlsx", base64.b64encode(output.getvalue()).decode())
        self.assertEqual("invalid_excel_file", caught.exception.code)
        self.assertIn("过多空白格式单元格", str(caught.exception))

    def test_formula_after_column_i_is_reported_per_row(self):
        content = excel_fixture.xlsx([excel_fixture.row() + [None, ("formula", "1+1")]])
        _, accepted, skipped, total = read_workbook("fixture.xlsx", content)
        self.assertEqual(1, total)
        self.assertEqual([], accepted)
        self.assertEqual("excel_formula_not_allowed", skipped[0]["code"])

    def test_nonempty_column_j_is_reported_per_row(self):
        content = excel_fixture.xlsx([excel_fixture.row() + [None, "accidental extra data"]])
        _, accepted, skipped, total = read_workbook("fixture.xlsx", content)
        self.assertEqual(1, total)
        self.assertEqual([], accepted)
        self.assertEqual("excel_extra_columns", skipped[0]["code"])

    def test_nonempty_column_j_header_is_rejected(self):
        content = excel_fixture.xlsx([excel_fixture.row()], headers=[*HEADERS, None, "extra"])
        with self.assertRaises(DomainError) as caught:
            read_workbook("fixture.xlsx", content)
        self.assertEqual("excel_headers_invalid", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
