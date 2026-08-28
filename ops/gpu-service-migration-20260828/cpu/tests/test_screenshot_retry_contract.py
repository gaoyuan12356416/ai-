"""Exercise the actual main-app retry contract without importing its runtime.

Only selected function definitions and literal screenshot specs are compiled.
Production DB, queues, callbacks, downloads and image generation are replaced by
in-memory mocks. Every filesystem operation stays in TemporaryDirectory. Set
SCREENSHOT_RETRY_APP_PATH to test a read-only deployed app.py instead of this
checkout's app.py. This is not a sidecar/cache or image-quality test.
"""

import ast
import concurrent.futures
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


APP_PATH = Path(os.environ.get(
    "SCREENSHOT_RETRY_APP_PATH", str(Path(__file__).resolve().parents[4] / "app.py")
))
FUNCTION_NAMES = {
    "file_ready", "retry_screenshot_job", "process_screenshot_job",
    "is_screenshot_source_consistency_rejection",
    "is_screenshot_generation_no_output_error",
    "is_screenshot_batch_recoverable_error", "is_screenshot_batch_fallback_error",
    "set_screenshot_batch_remake_progress", "set_screenshot_consistency_remake_progress",
    "cleanup_screenshot_output_paths",
}


def load_contract():
    # Do not import app: its module-level configuration/threads are production code.
    source = APP_PATH.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(APP_PATH))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_NAMES]
    if {node.name for node in functions} != FUNCTION_NAMES:
        raise AssertionError("Main-app screenshot contract changed; review test extraction")
    specs_nodes = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "SCREENSHOT_SPECS"
                           for target in node.targets)]
    if len(specs_nodes) != 1:
        raise AssertionError("Expected one literal SCREENSHOT_SPECS definition")
    specs = ast.literal_eval(specs_nodes[0].value)
    code = compile(ast.Module(body=functions, type_ignores=[]), str(APP_PATH), "exec")
    return code, specs


class ScreenshotRetryContractTests(unittest.TestCase):
    def test_failed_dimension_retry_keeps_successful_urls_files_and_sha(self):
        code, specs = load_contract()
        self.assertEqual(len(specs), 3, "Review fixture when production dimensions change")
        with tempfile.TemporaryDirectory(prefix="screenshot-retry-contract-") as folder:
            root = Path(folder)
            job_id = "0" * 32
            work = root / "work"
            public = root / "public"
            source = work / job_id / "source" / "cover_source.jpg"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"local source fixture; image validation is mocked")
            job = {
                "job_id": job_id, "status": "queued", "app_id": 0,
                "content_id": "offline-fixture", "cover_source_url": "https://fixture.invalid/source.jpg",
                "assets": {},
            }
            database = {}
            queued = []
            batch_calls = []
            single_calls = []
            allow_failed_dimension = False
            failed_key = specs[-1]["key"]

            def persist(value):
                database[value["job_id"]] = copy.deepcopy(value)

            def progress(value, **fields):
                fields.pop("persist", None)
                value.update(fields)
                persist(value)

            def emit(item):
                payload = ("offline-generated-" + item["key"]).encode("utf-8")
                for name in ("workspace_output_path", "public_output_path"):
                    path = Path(item[name])
                    # Test fixture failure must never escape its private directory.
                    path.resolve().relative_to(root.resolve())
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)

            def batch(_job, _source, items):
                batch_calls.append([item["key"] for item in items])
                for item in items:
                    if item["key"] != failed_key:
                        emit(item)
                raise RuntimeError("screenshot batch incomplete: " + failed_key)

            def single(_job, _source, items):
                self.assertEqual(len(items), 1)
                item = items[0]
                single_calls.append(item["key"])
                if item["key"] == failed_key and not allow_failed_dimension:
                    raise RuntimeError("offline forced dimension failure")
                emit(item)

            namespace = {
                "os": os, "concurrent": concurrent, "logging": mock.Mock(),
                "SCREENSHOT_WORK_ROOT": str(work), "SCREENSHOT_PUBLIC_ROOT": str(public),
                "SCREENSHOT_PUBLIC_BASE_URL": "https://fixture.invalid",
                "SCREENSHOT_SPECS": specs, "CODEX_SCREENSHOT_BATCH_ENABLED": True,
                "CODEX_SCREENSHOT_BATCH_STRICT": True, "SCREENSHOT_ITEM_RETRY_ATTEMPTS": 1,
                "ensure_dir": lambda path: Path(path).mkdir(parents=True, exist_ok=True),
                "image_file_ready": mock.Mock(return_value=True),
                "validate_screenshot_request": mock.Mock(side_effect=AssertionError("unexpected lookup")),
                "download_file": mock.Mock(side_effect=AssertionError("unexpected network download")),
                "remove_file_quietly": mock.Mock(side_effect=AssertionError("unexpected source removal")),
                "publish_asset": lambda path: "https://fixture.invalid/" + Path(path).name,
                "generate_screenshot_via_codex_service_batch": batch,
                "generate_screenshot_via_codex_service": single,
                "set_screenshot_job_progress": progress,
                "clamp_progress": lambda value: max(0, min(100, int(value))),
                "upsert_screenshot_job_record": persist,
                "fetch_screenshot_job_row": lambda key: copy.deepcopy(database.get(key)),
                "clear_screenshot_job_deleted_marker": mock.Mock(),
                "run_screenshot_job_async": lambda value: queued.append(copy.deepcopy(value)),
                "notify_screenshot_ai_source_callback": mock.Mock(),
                "finish_screenshot_job_run": mock.Mock(),
            }
            exec(code, namespace)

            # The real batch fallback persists two successes; only the missing
            # dimension is attempted twice and exhausts its item retry budget.
            with self.assertRaisesRegex(RuntimeError, "offline forced dimension failure"):
                namespace["process_screenshot_job"](job)
            self.assertEqual(single_calls, [failed_key, failed_key])
            self.assertEqual(set(job["assets"]), {spec["key"] for spec in specs[:-1]})
            namespace["notify_screenshot_ai_source_callback"].assert_not_called()

            successful_paths = []
            for spec in specs[:-1]:
                successful_paths.extend([
                    work / job_id / "generated" / spec["filename"],
                    public / job_id / spec["filename"],
                ])

            def file_evidence():
                return {str(path): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                        for path in successful_paths}

            original_files = file_evidence()
            original_urls = {spec["field"]: job[spec["field"]] for spec in specs[:-1]}

            # The outer async runner/DB are mocked. Model its persisted failed
            # status explicitly; do not claim this test covers worker scheduling.
            job["status"] = "failed"
            persist(job)
            result = namespace["retry_screenshot_job"](job_id)
            self.assertFalse(result["force_remake"])
            self.assertEqual((result["preserved_count"], result["retry_count"]), (2, 1))
            self.assertEqual(file_evidence(), original_files)
            self.assertEqual(len(queued), 1)

            allow_failed_dimension = True
            resumed = queued[0]
            before_retry_calls = len(single_calls)
            namespace["process_screenshot_job"](resumed)
            self.assertEqual(single_calls[before_retry_calls:], [failed_key])
            self.assertEqual(len(batch_calls), 1, "Retry must not resubmit successful dimensions as a batch")
            self.assertEqual(file_evidence(), original_files)
            self.assertEqual({field: resumed[field] for field in original_urls}, original_urls)
            self.assertEqual(resumed["status"], "done")
            self.assertEqual(set(resumed["assets"]), {spec["key"] for spec in specs})
            namespace["notify_screenshot_ai_source_callback"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
