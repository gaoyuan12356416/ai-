"""Apply the deploy patch to a disposable app and run the real full suite.

This validation is intentionally outside unittest discovery so the full suite
can be launched against the patched temporary ``app.py`` without recursion.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(command, cwd, env=None):
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    repo = Path(__file__).resolve().parents[1]
    deploy_script = repo / "deploy" / "apply_ad_control_execution_log_fix.py"
    source_app = repo / "app.py"
    with tempfile.TemporaryDirectory(prefix="ad-control-deploy-validation-") as temp_value:
        temp_root = Path(temp_value)
        target_app = temp_root / "app.py"
        shutil.copy2(source_app, target_app)
        original_hash = sha256(source_app)

        first = run([sys.executable, str(deploy_script), "--root", str(temp_root)], repo)
        backups = sorted((temp_root / "deploy_backups").glob("app.py.before-execution-log-*") )
        first_changed = "app.py: changed" in first.stdout
        first_unchanged = "app.py: unchanged" in first.stdout
        if first_changed == first_unchanged:
            raise AssertionError("first apply did not report exactly one changed/unchanged state")
        expected_backup_count = 1 if first_changed else 0
        if len(backups) != expected_backup_count:
            raise AssertionError(
                "first apply backup count mismatch: expected %s got %s"
                % (expected_backup_count, len(backups))
            )
        if first_changed and sha256(backups[0]) != original_hash:
            raise AssertionError("first-apply backup checksum differs from source app")
        if first_unchanged and sha256(target_app) != original_hash:
            raise AssertionError("unchanged first apply modified target bytes")

        first_patched_hash = sha256(target_app)
        second = run([sys.executable, str(deploy_script), "--root", str(temp_root)], repo)
        backups_after_second = sorted(
            (temp_root / "deploy_backups").glob("app.py.before-execution-log-*")
        )
        if len(backups_after_second) != expected_backup_count:
            raise AssertionError("idempotent apply created another backup")
        if sha256(target_app) != first_patched_hash:
            raise AssertionError("second apply changed patched app bytes")
        if "app.py: unchanged" not in second.stdout:
            raise AssertionError("second apply did not report unchanged")

        run([sys.executable, "-m", "py_compile", str(target_app)], temp_root)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        env["PYTHONPYCACHEPREFIX"] = str(temp_root / "pycache")
        suite = run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(repo / "tests"),
                "-p",
                "test_*.py",
            ],
            temp_root,
            env=env,
        )
        if "OK" not in suite.stdout:
            raise AssertionError("patched-app full suite did not report OK")
        print(first.stdout.strip())
        print(second.stdout.strip())
        print(suite.stdout.strip())
        print("patched_app_sha256=%s" % first_patched_hash)
        print("source_sha256=%s" % original_hash)
        print("first_apply_state=%s" % ("changed" if first_changed else "unchanged"))
        if first_changed:
            print("backup_sha256=%s" % original_hash)


if __name__ == "__main__":
    main()
