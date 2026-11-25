"""Release updates to the two independent TT pages without switching the reporting services."""

import argparse
import fcntl
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request

import deploy


def git_contents(commit, path):
    output = subprocess.run(
        ["git", "-C", str(deploy.SRC), "show", commit + ":" + path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return output.stdout if output.returncode == 0 else None


def stage(folder, commit):
    if (folder / "page-update.json").exists():
        return
    previous = deploy.CURRENT.resolve(strict=True)
    base = previous.parent.name
    if not re.fullmatch("[a-f0-9]{40}", base) or previous.name != "runtime":
        raise ValueError("cannot identify current promotion release")
    package = Path("features/tt_promotion")
    for file in (previous / package).glob("*.py"):
        if file.read_bytes() != git_contents(base, (package / file.name).as_posix()):
            raise ValueError("live promotion module changed: " + file.name)
    runtime = folder / "runtime"
    shutil.copytree(
        previous,
        runtime,
        symlinks=True,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tt_promotion"),
    )
    shutil.copytree(
        deploy.SRC / package,
        runtime / package,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    records = []
    for path in sorted((deploy.SRC / "static").glob("tt-promotion*")):
        data = path.read_bytes()
        old = git_contents(base, "static/" + path.name)
        for root in (deploy.MAIN / "static", deploy.PUBLIC):
            target = root / path.name
            actual = deploy.read(target)
            if actual == data:
                continue
            if actual != old:
                raise ValueError("concurrent static change: " + str(target))
            index = len(records)
            if actual is not None:
                deploy.atomic(folder / "before" / str(index), actual)
            deploy.atomic(folder / "stage" / str(index), data)
            records.append(
                dict(
                    target=str(target),
                    index=index,
                    before=deploy.sha(old) if old is not None else None,
                    after=deploy.sha(data),
                )
            )
    # Check and validate the imported modules using the same Python as the service.
    deploy.run(
        "runuser",
        "-u",
        "tt-post",
        "--",
        "/usr/bin/python3",
        "-B",
        "-c",
        "from features.tt_promotion import operations,http_server,publishing,profiles",
        cwd=runtime,
    )
    state = dict(
        commit=commit,
        files=records,
        link=dict(link=str(deploy.CURRENT), before=str(previous), after=str(runtime)),
    )
    deploy.atomic(folder / "page-update.json", deploy.json_bytes(state), 0o600)


def template_versions():
    path = deploy.DATA / "promotion.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return {
            str(k): v
            for k, v in db.execute("SELECT id,current_version FROM tt_auto_template WHERE enabled=1")
        }


def rollback_ready(before):
    with sqlite3.connect((deploy.DATA / "promotion.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        outstanding = db.execute("""SELECT 1 FROM ttp_slot s LEFT JOIN tt_auto_task t
            ON t.run_id=s.native_run_id AND t.account_id=s.account_id
            WHERE s.state IN ('planned','reserved') OR t.unknown_outcome=1 OR
            (s.state='enqueued' AND COALESCE(t.status,'') NOT IN ('published','failed','skipped','no_candidate','canceled','cancelled','missed')) LIMIT 1""").fetchone()
    if outstanding or any(before.get(k) != v for k, v in template_versions().items()):
        raise ValueError(
            "finish or reconcile outstanding jobs and pause new-version templates before rollback"
        )


def switch(folder, rollback):
    manifest = json.loads((folder / "page-update.json").read_text())
    link = manifest["link"]
    records = manifest["files"]
    if str(deploy.CURRENT.resolve()) not in (link["before"], link["after"]):
        raise ValueError("a later promotion version is already live")
    for record in records:
        if deploy.blob(Path(record["target"])) not in (record["before"], record["after"]):
            raise ValueError("later file change preserved: " + record["target"])
    baseline = folder / "enabled-before.json"
    if rollback:
        rollback_ready(json.loads(baseline.read_text()))
    elif not baseline.exists():
        deploy.atomic(baseline, deploy.json_bytes(template_versions()), 0o600)
    if not rollback:
        deploy.init_db(folder)
    for record in records:
        deploy.file_switch(record, folder, rollback)
    deploy.switch_link(link, rollback)
    deploy.run("systemctl", "restart", "tt-promotion.service")
    for _ in range(25):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18920/health", timeout=2) as res:
                if res.status == 200 and json.load(res).get("ok"):
                    print("active runtime", deploy.CURRENT.resolve())
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.4)
    raise RuntimeError("promotion API did not start")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stage", "activate", "rollback"))
    parser.add_argument("commit")
    args = parser.parse_args()
    commit = subprocess.check_output(["git", "-C", str(deploy.SRC), "rev-parse", "HEAD"], text=True).strip()
    if args.commit != commit or not re.fullmatch("[a-f0-9]{40}", commit):
        raise ValueError("wrong source commit")
    folder = deploy.DEPLOY / commit
    folder.mkdir(mode=0o755, exist_ok=True)
    for private in ("before", "stage"):
        (folder / private).mkdir(mode=0o700, exist_ok=True)
    with open(deploy.DEPLOY / "promotion-update.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "stage":
            stage(folder, commit)
        else:
            switch(folder, args.action == "rollback")


if __name__ == "__main__":
    main()
