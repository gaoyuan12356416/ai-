"""Validate check/apply compatibility against the refreshed production app.

The source fixture is never modified.  The validator copies it into a temporary
root, proves check mode is read-only, applies the V2 patch with a byte-identical
backup, verifies the 2026-07-15 writer/reader safety chain was preserved, and
then proves a second check/apply is idempotent.
"""

import argparse
import difflib
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deploy import apply_ad_control_execution_log_fix as deploy_fix


PRESERVED_SAFETY_FUNCTIONS = (
    "ad_control_action_log_writer_config",
    "ad_control_action_log_reader_config",
    "ad_control_local_action_log_row",
    "ad_control_persist_action_log",
    "ad_control_update_action_log_runner",
    "ad_control_action_log_utc_bound",
    "ad_control_mysql_action",
)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def run(command, cwd):
    return subprocess.run(
        command,
        cwd=str(cwd),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def function_source(text, name):
    matches = deploy_fix.function_matches(text, name)
    if len(matches) != 1:
        raise AssertionError("fixture function %s count=%s" % (name, len(matches)))
    return matches[0].group(0).rstrip()


def assert_live_fixture_guard(text):
    required = (
        'AD_CONTROL_ACTION_LOG_DB_NAME = "ads_ai"',
        'AD_CONTROL_ACTION_LOG_TABLE = "ad_control_action_log"',
        'AD_CONTROL_ACTION_LOG_MYSQL_PORT = (os.environ.get("AD_CONTROL_ACTION_LOG_MYSQL_PORT") or "63353").strip()',
        'AD_CONTROL_ACTION_LOG_READER_MYSQL_PORT = (os.environ.get("AD_CONTROL_ACTION_LOG_READER_MYSQL_PORT") or "63350").strip()',
        'AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT", "3"))',
        'AD_CONTROL_ACTION_LOG_IO_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_IO_TIMEOUT", "5"))',
        'AD_CONTROL_LIVE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_MAX_WORKERS", "4"))',
        "def ad_control_action_log_writer_config():",
        "def ad_control_action_log_reader_config():",
        "ad_control_action_log_writer_config(), record, AD_CONTROL_ACTION_LOG_TABLE",
        "ad_control_action_log_reader_config(), action_id, AD_CONTROL_ACTION_LOG_TABLE",
    )
    for token in required:
        if text.count(token) != 1:
            raise AssertionError("live fixture guard token count != 1: %s" % token)
    for token in (
        "def ad_control_action_log_config():",
        "retrying upsert",
        'AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_CONNECT_TIMEOUT", "5"))',
        'AD_CONTROL_ACTION_LOG_IO_TIMEOUT = int(os.environ.get("AD_CONTROL_ACTION_LOG_IO_TIMEOUT", "8"))',
        'AD_CONTROL_LIVE_MAX_WORKERS = int(os.environ.get("AD_CONTROL_LIVE_MAX_WORKERS", "12"))',
    ):
        if token in text:
            raise AssertionError("live fixture contains regressed guard token: %s" % token)


def assert_observe_audit_contract(text):
    list_source = function_source(text, "list_ad_control_actions")
    for token in (
        'observe_mode = str(criteria.get("run_mode") or "").strip().lower() == "observe"',
        '{"key": "observed", "label": "观察完成", "class": "ok"}',
        'mode = "observe" if observe_mode else "dry-run" if item.get("dry_run") else "real"',
        'mode_label = "只观察" if observe_mode else "Dry-run 试跑" if item.get("dry_run") else "正式执行"',
        '"mode": mode',
        '"mode_label": mode_label',
    ):
        if token not in list_source:
            raise AssertionError("patched live list action lost observe audit token: %s" % token)
    detail_source = function_source(text, "ad_control_action_audit")
    for token in (
        'observe_mode = str(criteria.get("run_mode") or "").strip().lower() == "observe"',
        '{"key": "observed", "label": "观察完成", "class": "ok"}',
        '"mode": "observe" if observe_mode else "dry-run" if item.get("dry_run") else "real"',
        '"mode_label": "只观察" if observe_mode else "Dry-run 试跑" if item.get("dry_run") else "正式执行"',
        '"copy": "复制"',
        '"mixed": "关闭/复制"',
    ):
        if token not in detail_source:
            raise AssertionError("patched live target audit lost observe token: %s" % token)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-app", required=True)
    args = parser.parse_args()

    repo = REPO_ROOT
    deploy_script = repo / "deploy" / "apply_ad_control_execution_log_fix.py"
    fixture = Path(args.live_app).resolve()
    source_bytes = fixture.read_bytes()
    source_hash = sha256_bytes(source_bytes)
    source_text = source_bytes.decode("utf-8").replace("\r\n", "\n")
    assert_live_fixture_guard(source_text)
    preserved_before = {
        name: sha256_bytes(function_source(source_text, name).encode("utf-8"))
        for name in PRESERVED_SAFETY_FUNCTIONS
    }

    fixture_check = run(
        [sys.executable, str(deploy_script), "--root", str(fixture.parent), "--check"],
        repo,
    )
    if sha256_bytes(fixture.read_bytes()) != source_hash:
        raise AssertionError("--check modified the live-final fixture")

    with tempfile.TemporaryDirectory(prefix="ad-control-live-final-compat-") as value:
        temp_root = Path(value)
        target = temp_root / "app.py"
        shutil.copy2(fixture, target)
        first = run([sys.executable, str(deploy_script), "--root", str(temp_root)], repo)
        patched_bytes = target.read_bytes()
        patched_hash = sha256_bytes(patched_bytes)
        patched_text = patched_bytes.decode("utf-8").replace("\r\n", "\n")
        deploy_fix.assert_action_log_safety_contract(patched_text)
        assert_live_fixture_guard(patched_text)
        assert_observe_audit_contract(patched_text)

        preserved_after = {
            name: sha256_bytes(function_source(patched_text, name).encode("utf-8"))
            for name in PRESERVED_SAFETY_FUNCTIONS
        }
        changed_safety_functions = [
            name for name in PRESERVED_SAFETY_FUNCTIONS
            if preserved_before[name] != preserved_after[name]
        ]
        if changed_safety_functions:
            raise AssertionError(
                "patch changed deployed safety functions: %s" % changed_safety_functions
            )

        backups = sorted((temp_root / "deploy_backups").glob("app.py.before-execution-log-*"))
        if len(backups) != 1 or sha256_bytes(backups[0].read_bytes()) != source_hash:
            raise AssertionError("first apply did not create one byte-identical backup")

        second_check = run(
            [sys.executable, str(deploy_script), "--root", str(temp_root), "--check"],
            repo,
        )
        if "app.py: unchanged" not in second_check.stdout:
            raise AssertionError("second check did not report unchanged")
        second = run([sys.executable, str(deploy_script), "--root", str(temp_root)], repo)
        if "app.py: unchanged" not in second.stdout:
            raise AssertionError("second apply did not report unchanged")
        if sha256_bytes(target.read_bytes()) != patched_hash:
            raise AssertionError("idempotent check/apply changed patched bytes")
        if len(list((temp_root / "deploy_backups").glob("app.py.before-execution-log-*"))) != 1:
            raise AssertionError("idempotent apply created another backup")
        run([sys.executable, "-m", "py_compile", str(target)], repo)

        diff = list(
            difflib.unified_diff(
                source_text.splitlines(), patched_text.splitlines(), lineterm=""
            )
        )
        print(fixture_check.stdout.strip())
        print(first.stdout.strip())
        print(second_check.stdout.strip())
        print(second.stdout.strip())
        print("live_fixture_sha256=%s" % source_hash)
        print("patched_sha256=%s" % patched_hash)
        print("unified_diff_lines=%s" % len(diff))
        print("preserved_safety_functions=%s" % len(PRESERVED_SAFETY_FUNCTIONS))
        print("changed_safety_functions=0")
        print("backup_count=1")
        print("idempotent_second_apply=true")
        print("observe_audit_contract=true")


if __name__ == "__main__":
    main()
