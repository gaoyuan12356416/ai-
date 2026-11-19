"""GitHub-source overlay and warm loopback handoff. Rollback switches code only."""

import argparse
import grp
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
PKG = HERE.parents[1]
ROOT = PKG.parent
CFG = Path("/etc/pipeline-coordinator.json")
GROUPS = {
    "fb": ("fb-auto-post-service", "/opt/fb-auto-post/current", "FB_AUTO_POST_PORT"),
    "x": ("x-post-automation", "/opt/x-post-automation/current", "X_OAUTH_PORT"),
    "tag": ("x-tag-post", "/opt/x-tag-publisher/current", "X_OAUTH_PORT"),
    "tt": ("tt-post-service", "/opt/tt-post/current", "TT_POST_SERVICE_PORT"),
    "tt_auto": (
        "tt-auto-post-service",
        "/opt/tt-auto-post/current",
        "TT_AUTO_POST_SERVICE_PORT",
    ),
    "coordinator": ("pipeline-coordinator", "/opt/pipeline-dispatch/current", ""),
    **{
        g: ("pipeline-cos-native-" + s, "", "")
        for g, s in (
            ("cos_fb", "fb"),
            ("cos_x", "x"),
            ("cos_tag", "x-tag"),
            ("cos_tt", "tt"),
            ("cos_tt_auto", "tt-auto"),
        )
    },
}


def run(*args, timeout=120):
    result = subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(
            f"{args[0]} failed ({result.returncode}): " + result.stderr[-320:]
        )
    return result.stdout.strip()


def atomic(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + "." + str(os.getpid()))
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    tmp.chmod(mode)
    os.replace(tmp, path)


def dump(path, data, mode=0o644):
    atomic(path, json.dumps(data, indent=2) + "\n", mode)


def read(path):
    return json.loads(Path(path).read_text())


def prop(unit, field):
    return run("systemctl", "show", "-p", field, "--value", unit)


def proc(unit):
    return Path("/proc") / str(int(prop(unit, "MainPID")))


def original_argv(p):
    return [a for a in (p / "cmdline").read_bytes().decode().split(chr(0)) if a]


def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def sockets(owner, port=None):
    rows = run("ss", "-Htanp", *([f"( sport = :{port} )"] if port else [])).splitlines()
    return [r.split() for r in rows if f"pid={owner}," in r]


def port_of(owner):
    ports = [
        int(row[3].rsplit(":", 1)[1])
        for row in sockets(owner)
        if row[0] == "LISTEN" and row[3].startswith("127.0.0.1:")
    ]
    if len(ports) != 1:
        raise RuntimeError("owning listener ambiguous")
    return ports[0]


def clone(old, new):
    if not new.exists():
        new.mkdir()
        run("cp", "-al", str(old) + "/.", new, timeout=240)


def stage(group, predecessor=None, inverse_of=None):
    folder = ROOT / group
    folder.mkdir(exist_ok=True)
    unit, link, port_env = GROUPS[group]
    unit += ".service"
    p = proc(unit)
    old = (p / "cwd").resolve()
    argv = original_argv(p)
    port = port_of(p.name)
    alt = 19140 + list(GROUPS).index(group)
    opts = {}
    service_args = list(argv)
    dest = folder / "candidate"
    if link:
        prior = Path(link).resolve()
        if not Path(link).is_symlink():
            raise RuntimeError("active source moved: " + group)
        if prior != old:
            # An earlier scheduler-only release can advance X Tag's code
            # pointer while its long-lived HTTP service retains the old cwd.
            manifest_path = prior.parent / "manifest.json"
            receipt = (
                read(manifest_path)
                if group == "tag" and manifest_path.is_file()
                else {}
            )
            if (
                receipt.get("status"),
                receipt.get("previous"),
                receipt.get("candidate"),
            ) != ("active", str(old), str(prior)):
                raise RuntimeError("active source moved: " + group)
        for i, v in enumerate(service_args):
            for prefix in (str(old), str(prior), link):
                if v.startswith(prefix + "/"):
                    service_args[i] = str(dest) + v[len(prefix) :]
                    break
    else:
        cfg = read(argv[argv.index("--config") + 1])
        prior = Path(cfg["overlay"]).resolve()
        cfg["overlay"] = str(dest)
        dump(folder / "native.json", cfg)
        dump(folder / "warm-native.json", dict(cfg, port=alt))
        service_args[service_args.index("--config") + 1] = str(folder / "native.json")
    if group in ("x", "tag"):
        # The stable entry point freezes LISTEN_PORT on import. The warm
        # listener installs the same handler without starting a scheduler.
        opts["warm_x"] = True
    if group == "tt_auto":
        opts["environment"] = {
            "PIPELINE_TT_VERIFIED_ORIGIN": "https://socialkit-cdn.yingliang.tech",
            "PIPELINE_MEDIA_PREFLIGHT_TMP": "/mnt/data-disk/social-480-20261119/tmp/tt-post",
        }
    if group in ("x", "tag"):
        opts["environment"] = {
            "PIPELINE_MEDIA_PREFLIGHT_TMP": "/mnt/data-disk/social-480-20261119/tmp/x-post-automation"
        }
    clone(prior, dest)
    checks = read(HERE / "manifest.json")[group]
    previous_checks = {}
    original = None
    if inverse_of:
        prior_release = Path(inverse_of).resolve()
        older = read(prior_release / group / "release.json")
        inherited = read(prior.parent / "release.json")
        if (
            group != "fb"
            or prior_release.parent != ROOT.parent
            or inherited.get("candidate") != str(prior)
            or inherited.get("previous") != older["candidate"]
        ):
            raise RuntimeError(
                "active FB successor is not based on the selected release"
            )
        original = Path(older["previous"])
        opts["inverse_of"] = str(prior_release)
    if predecessor:
        predecessor = Path(predecessor).resolve()
        if (
            predecessor.parent != ROOT.parent
            or not link
            or Path(read(predecessor / group / "release.json")["candidate"]) != prior
        ):
            raise RuntimeError("predecessor is not the active release")
        previous_checks = read(
            predecessor / "package" / HERE.relative_to(PKG) / "manifest.json"
        )[group]
    for name, entry in checks.items():
        rel = Path(name) if link else Path(name).name
        live = prior / rel
        actual = blob(live.read_bytes()) if live.is_file() else None
        if original:
            before = original / rel
            if (
                actual != entry["after"]
                or (blob(before.read_bytes()) if before.is_file() else None)
                != entry["before"]
            ):
                raise RuntimeError("overlapping successor change: " + name)
            if before.is_file():
                atomic(dest / rel, before.read_bytes())
            else:
                (dest / rel).unlink(missing_ok=True)
            continue
        if actual != previous_checks.get(name, {}).get("after", entry["before"]):
            raise RuntimeError("current source drift: " + group + " " + name)
        content = (PKG / entry["file"]).read_bytes()
        if blob(content) != entry["after"]:
            raise RuntimeError("unverified Git source: " + name)
        if name.endswith(".py"):
            compile(content, name, "exec")
        atomic(dest / rel, content)
    meta = dict(
        group=group,
        unit=unit,
        link=link,
        old=str(old),
        previous=str(prior),
        candidate=str(dest),
        port=port,
        alt=alt,
        port_env=port_env,
        argv=service_args,
        previous_pid=p.name,
        **opts,
    )
    dump(folder / "release.json", meta)
    backup = folder / "backup"
    backup.mkdir(mode=0o700, exist_ok=True)
    atomic(backup / "unit.txt", run("systemctl", "cat", unit), 0o600)
    dump(backup / "preimages.json", checks, 0o600)
    atomic(
        folder / "rollback.sh",
        "#!/bin/sh\nset -eu\nexec /usr/bin/python3 "
        + shlex.quote(str(__file__))
        + " rollback "
        + group
        + "\n",
        0o700,
    )
    if original:
        atomic(
            folder / "revert-scheduled-media.sh",
            "#!/bin/sh\nset -eu\nexec /usr/bin/python3 "
            + shlex.quote(str(__file__))
            + " apply-inverse fb\n",
            0o700,
        )
    return meta


def warm_start(group, direction):
    m = read(ROOT / group / "release.json")
    p = proc(m["unit"])
    env = dict(
        v.split("=", 1)
        for v in (p / "environ").read_bytes().decode().split(chr(0))
        if "=" in v
    )
    for key in (
        "NOTIFY_SOCKET",
        "WATCHDOG_PID",
        "WATCHDOG_USEC",
        "LISTEN_PID",
        "LISTEN_FDS",
    ):
        env.pop(key, None)
    revert = direction == "rollback"
    cwd = Path((m["previous"] if revert else m["candidate"]) if m["link"] else m["old"])
    argv = list(m["argv"])
    if revert and m["link"]:
        argv = [
            str(m["previous"]) + a[len(m["candidate"]) :]
            if a.startswith(m["candidate"] + "/")
            else a
            for a in argv
        ]
    if not revert:
        env.update(m.get("environment", {}))
    if group == "coordinator":
        argv += ["--standby-port", str(m["alt"])]
    elif group.startswith("cos_"):
        cfg = read(ROOT / group / "warm-native.json")
        if revert:
            cfg["overlay"] = m["previous"]
            dump(ROOT / group / "warm-rollback.json", cfg)
        argv[argv.index("--config") + 1] = str(
            ROOT / group / ("warm-rollback.json" if revert else "warm-native.json")
        )
    elif m.get("warm_x") or group == "tt_auto":
        module = {
            "x": "features.x_accounts.oauth_service",
            "tag": "features.x_tag_publish.runtime",
            "tt_auto": "scripts.tt_auto_post_service",
        }[group]
        script = (
            "import http.server as h,runpy\noriginal=h.ThreadingHTTPServer.__init__\n"
            "def bind(server,addr,*a,**kw):\n"
            f"    if addr==('127.0.0.1',{m['port']}):addr=('127.0.0.1',{m['alt']})\n"
            "    original(server,addr,*a,**kw)\n"
            "h.ThreadingHTTPServer.__init__=bind\n"
            + f"runpy.run_module({module!r},run_name='__main__')\n"
        )
        argv = [argv[0], "-c", script]
    else:
        env[m["port_env"]] = str(m["alt"])
    os.chdir(cwd)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    owner = prop(m["unit"], "User")
    if owner and owner != "root":
        u = pwd.getpwnam(owner)
        gid = (
            grp.getgrnam(prop(m["unit"], "Group")).gr_gid
            if prop(m["unit"], "Group")
            else u.pw_gid
        )
        os.initgroups(owner, gid)
        os.setgid(gid)
        os.setuid(u.pw_uid)
    os.execve(argv[0], argv, env)


def status(port, direct=False):
    conn = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=3,
        source_address=("127.0.0.2", 0) if direct else None,
    )
    try:
        conn.request("GET", "/health")
        res = conn.getresponse()
        res.read(32768)
        return res.status
    except (OSError, http.client.HTTPException):
        return 0
    finally:
        conn.close()


def wait(test, seconds, reason):
    end = time.monotonic() + seconds
    while not test():
        if time.monotonic() >= end:
            raise RuntimeError(reason)
        time.sleep(0.35)


def redirect(m, action):
    rule = [
        "OUTPUT",
        "-p",
        "tcp",
        "-d",
        "127.0.0.1/32",
        "!",
        "-s",
        "127.0.0.2/32",
        "--dport",
        str(m["port"]),
        "-m",
        "comment",
        "--comment",
        "scheduled-media-warm-" + m["group"],
        "-j",
        "REDIRECT",
        "--to-ports",
        str(m["alt"]),
    ]
    run("iptables", "-w", "5", "-t", "nat", action, *rule)


def point(link, new):
    tmp = link.with_name(".media-switch-" + str(os.getpid()))
    tmp.symlink_to(new)
    os.replace(tmp, link)


def switch(group, revert=False):
    folder = ROOT / group
    m = read(folder / "release.json")
    unit = m["unit"]
    p = proc(unit)
    old_pid = p.name
    direction = "rollback" if revert else "activate"
    drop = Path("/etc/systemd/system") / (unit + ".d") / "zzzzzz-social-media-480.conf"
    temp = (
        Path("/run/systemd/system")
        / (unit + ".d")
        / "zzzzzzz-social-media-http-handoff.conf"
    )
    origin = m["candidate"] if revert else m["previous"]
    target = m["previous"] if revert else m["candidate"]
    link = Path(m["link"])
    if m["link"]:
        expected_runtime = Path(m["candidate"] if revert else m["old"])
        if link.resolve() != Path(origin) or (p / "cwd").resolve() != expected_runtime:
            raise RuntimeError("owner or code pointer changed: " + group)
    elif not revert and drop.exists():
        raise RuntimeError("cloud unit override already exists")
    elif revert and not drop.exists():
        raise RuntimeError("cloud release is not the active version")
    if (
        temp.exists()
        or (Path("/run/pipeline-service-drain") / (unit + ".json")).exists()
    ):
        raise RuntimeError("concurrent service handoff")
    if port_of(old_pid) != m["port"]:
        raise RuntimeError("listener changed")
    health = status(m["port"], True)
    if not 200 <= health < 500:
        raise RuntimeError("stable endpoint unavailable")
    warm = "social-480-warm-" + group.replace("_", "-")
    before_deps = {
        u: prop(u, "MainPID")
        for u in prop(unit, "RequiredBy").split()
        if prop(u, "ActiveState") == "active"
        and (not u.endswith(".service") or prop(u, "Type") != "oneshot")
    }
    run(
        "systemd-run",
        "--collect",
        "--unit=" + warm,
        "--property=TimeoutStopSec=300",
        "--property=UMask=0077",
        sys.executable,
        __file__,
        "warm",
        group,
        direction,
    )
    redirected = False
    switched = False
    ready = False
    try:
        wait(
            lambda: status(m["alt"]) == health,
            60,
            "standby failed to serve the existing health endpoint",
        )
        warm_pid = prop(warm, "MainPID")
        if port_of(warm_pid) != m["alt"]:
            raise RuntimeError("unexpected standby listener")
        redirect(m, "-I")
        redirected = True
        wait(
            lambda: (
                not any(
                    row[0] != "LISTEN" or int(row[1])
                    for row in sockets(old_pid, m["port"])
                )
            ),
            180,
            "existing HTTP requests still active",
        )
        if p != proc(unit) or (m["link"] and link.resolve() != Path(origin)):
            raise RuntimeError("unit changed while draining")
        if prop(unit, "ExecStop"):
            # Every canonical RPC and heartbeat is being served by the warm
            # owner with the same ledger. Restore the ordinary gate immediately
            # after the stable listener is ready.
            atomic(temp, "[Service]\nExecStop=\n")
        before = drop.read_bytes() if drop.exists() else None
        if m["link"]:
            point(link, target)
            if m.get("environment"):
                if revert:
                    drop.unlink(missing_ok=True)
                else:
                    atomic(
                        drop,
                        "[Service]\n"
                        + "".join(
                            "Environment=" + k + "=" + v + "\n"
                            for k, v in m["environment"].items()
                        ),
                    )
        elif revert:
            drop.unlink()
        else:
            filtered = [
                line
                for line in prop(unit, "ExecStart")
                .split("argv[]=")[-1]
                .split(" ; ")[0]
                .split()
                if line
            ]
            launcher = next(v for v in filtered if v.endswith("pipeline_cos_launch.py"))
            original_cfg = read(filtered[-1])
            launch = dict(original_cfg, runtime=m["old"], argv=m["argv"])
            dump(folder / "launch.json", launch, 0o600)
            atomic(
                drop,
                "[Service]\nExecStart=\nExecStart=/usr/bin/python3 "
                + launcher
                + " "
                + str(folder / "launch.json")
                + "\n",
            )
        switched = True
        run("systemctl", "daemon-reload")
        run("systemctl", "--job-mode=ignore-dependencies", "restart", unit, timeout=120)
        wait(
            lambda: status(m["port"], True) == health,
            60,
            "stable owner has not resumed",
        )
        current = proc(unit)
        if current.name == old_pid or (
            m["link"] and (current / "cwd").resolve() != Path(target)
        ):
            raise RuntimeError("new stable process loaded unexpected release")
        ready = True
        for dep in before_deps:
            if prop(dep, "ActiveState") not in ("active", "activating"):
                run("systemctl", "start", "--no-block", dep)
        dump(
            folder / (direction + "-receipt.json"),
            dict(
                before=old_pid,
                after=current.name,
                health=health,
                switch=target,
                dependencies={k: prop(k, "ActiveState") for k in before_deps},
            ),
        )
    except BaseException:
        if switched and not ready:
            if m["link"]:
                point(link, origin)
            if before is None:
                drop.unlink(missing_ok=True)
            else:
                atomic(drop, before)
            run("systemctl", "daemon-reload")
            run(
                "systemctl",
                "--job-mode=ignore-dependencies",
                "restart",
                unit,
                timeout=120,
            )
            wait(
                lambda: status(m["port"], True) == health,
                60,
                "stable rollback not ready",
            )
            ready = True
        raise
    finally:
        if redirected and (not switched or ready):
            redirect(m, "-D")
            redirected = False
        if not redirected:
            temp.unlink(missing_ok=True)
            run("systemctl", "daemon-reload")
            w = prop(warm, "MainPID")
            if w and w != "0":
                wait(
                    lambda: (
                        not any(
                            row[0] != "LISTEN" or int(row[1])
                            for row in sockets(w, m["alt"])
                        )
                    ),
                    300,
                    "standby still completing accepted requests",
                )
                run("systemctl", "stop", warm)
    print(group, direction, "verified", flush=True)


def supply_original(previous):
    """Add the lazy file-verification dependency to already switched HTTP owners."""
    previous = Path(previous).resolve()
    if previous.parent != ROOT.parent or not previous.name.startswith("release-"):
        raise ValueError("unexpected release directory")
    manifest = read(HERE / "manifest.json")
    rel = "features/pipeline_dispatch/original_media.py"
    for group in ("fb", "x", "tag", "tt"):
        meta = read(previous / group / "release.json")
        dest = Path(meta["candidate"])
        applied = read(previous / group / "activate-receipt.json")
        if (
            applied["switch"] != str(dest)
            or Path(meta["link"]).resolve() != dest
            or (proc(meta["unit"]) / "cwd").resolve() != dest
        ):
            raise RuntimeError("native release changed: " + group)
        source = manifest[group][rel]
        data = (PKG / source["file"]).read_bytes()
        actual = dest / rel
        if blob(data) != source["after"] or source["before"] is not None:
            raise RuntimeError("unexpected dependency preimage")
        if not actual.is_file():
            # Link without replacing a concurrent deployment's file.
            tmp = actual.with_name(".original-media-" + str(os.getpid()))
            atomic(tmp, data)
            try:
                os.link(tmp, actual)
            finally:
                tmp.unlink(missing_ok=True)
        if blob(actual.read_bytes()) != source["after"]:
            raise RuntimeError("conflicting original-media dependency")
        dump(
            previous / group / "dependency-receipt.json",
            {"file": rel, "blob": source["after"], "release": str(ROOT)},
        )
        print(group, "dependency verified", flush=True)


def refresh_timing():
    """Reproject existing unclaimed FB release-timed offers from their native rows."""
    m = read(ROOT / "coordinator/release.json")
    p = proc(m["unit"])
    if (p / "cwd").resolve() != Path(m["candidate"]):
        raise RuntimeError("coordinator release changed")
    sys.path.insert(0, m["candidate"])
    from contextlib import closing
    from features.pipeline_dispatch.core import DispatchStore, stamp
    from features.pipeline_dispatch.owners import OwnerResolver
    from features.pipeline_dispatch.service import build_registry

    cfg = read(CFG)
    env = dict(
        v.split("=", 1)
        for v in (p / "environ").read_bytes().decode().split(chr(0))
        if "=" in v
    )
    for driver in cfg["drivers"]:
        key = driver["token_env"]
        os.environ[key] = env[key]
    adapter = build_registry(cfg).adapters[("fb", "auto")]
    owners = OwnerResolver(cfg["paths"].get("youtube"))
    store = DispatchStore(
        cfg["db_path"], capability_exclusions=cfg.get("node_capability_exclusions")
    )
    with closing(store.connect()) as c:
        rows = c.execute("""SELECT source_id,scheduled_at FROM pipeline_job WHERE channel='fb' AND family='auto'
            AND stage='prepare' AND status IN ('queued','ready') AND generation=0 AND hold_reason=''
            AND external_intent=0 AND claim_hash='' AND canonical_receipt=''""").fetchall()
    refreshed = 0
    for row in rows:
        native = adapter.read(row["source_id"])
        if not native:
            continue
        offer = adapter.project(native)
        if (
            offer["stage"] == "prepare"
            and offer["status"] in ("queued", "ready")
            and stamp(offer["scheduled_at"]) < row["scheduled_at"]
        ):
            for resolved in owners.resolve([offer]):
                store.offer(resolved)
                refreshed += 1
    result = {"observed": len(rows), "refreshed": refreshed}
    dump(ROOT / "timing-refresh.json", result)
    print(result, flush=True)


def backup():
    cfg = read(CFG)
    out = ROOT / "ledger-backup"
    out.mkdir(mode=0o700, exist_ok=True)
    paths = dict(cfg["paths"], dispatch=cfg["db_path"])
    paths["tag"] = cfg["x_tag"]["paths"]["x"]
    for name in ("dispatch", "fb", "tt_auto", "tt", "x", "tag"):
        dst = out / (name + ".sqlite3")
        if dst.exists():
            continue
        tmp = dst.with_suffix(".pending")
        with (
            sqlite3.connect(
                Path(paths[name]).as_uri() + "?mode=ro", uri=True, timeout=15
            ) as src,
            sqlite3.connect(tmp) as cp,
        ):
            # An explicit WAL snapshot lets the online backup keep moving
            # while active workers continue to update the source database.
            src.execute("BEGIN")
            src.execute("SELECT rootpage FROM sqlite_master LIMIT 1").fetchone()
            src.backup(cp, pages=6000, sleep=0.03)
        tmp.chmod(0o600)
        os.replace(tmp, dst)
        print("backed up", name, flush=True)


def mirror():
    common = Path("/mnt/data-disk/social-480-20261119/tmp")
    for user in ("pipeline-worker", "tt-post", "x-post-automation"):
        w = common / user
        w.mkdir(parents=True, exist_ok=True)
        userinfo = pwd.getpwnam(user)
        os.chown(w, userinfo.pw_uid, userinfo.pw_gid)
        w.chmod(0o700)
    unit = Path("/etc/systemd/system")
    fname = "pipeline-tt-original"
    environ = dict(
        v.split("=", 1)
        for v in (proc("pipeline-node-cpu-main") / "environ")
        .read_bytes()
        .decode()
        .split(chr(0))
        if "=" in v
    )
    names = [
        "TT_POST_GPU_COS_" + n
        for n in ("REGION", "DOMAIN", "BUCKET", "PREFIX", "SECRET_ID", "SECRET_KEY")
    ]
    credential_file = ROOT / "tt-original-storage.env"
    if any(
        not re.fullmatch(r"[A-Za-z0-9/:._+@=-]+", environ.get(k, "")) for k in names
    ):
        raise RuntimeError("existing TT storage settings unavailable")
    atomic(credential_file, "".join(k + "=" + environ[k] + "\n" for k in names), 0o600)
    service = (
        "[Unit]\nDescription=Copy a checked original to TikTok's verified media origin\n"
        "[Service]\nUser=pipeline-worker\nGroup=pipeline-worker\nStandardInput=socket\nStandardOutput=socket\nStandardError=journal\n"
        "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nReadWritePaths="
        + str(common / "pipeline-worker")
        + "\n"
        + "EnvironmentFile="
        + str(credential_file)
        + "\n"
        + "Environment=PIPELINE_MEDIA_PREFLIGHT_TMP="
        + str(common / "pipeline-worker")
        + "\nWorkingDirectory="
        + str(ROOT / "tt_auto/candidate")
        + "\n"
        + "ExecStart=/usr/bin/python3 -B -m features.pipeline_dispatch.original_media\n"
    )
    sock = (
        "[Unit]\nDescription=TikTok original media copy\n[Socket]\nListenStream=/run/pipeline-tt-original.sock\n"
        "SocketUser=tt-post\nSocketGroup=tt-post\nSocketMode=0600\nAccept=yes\nMaxConnections=12\n"
        "[Install]\nWantedBy=sockets.target\n"
    )
    for name, data in ((fname + "@.service", service), (fname + ".socket", sock)):
        path = unit / name
        if path.exists() and path.read_text() != data:
            raise RuntimeError("parallel original-media socket configuration")
        atomic(path, data)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", fname + ".socket")


def rollback(group, inverse=False):
    cfg = read(CFG)
    if inverse and (
        group != "fb" or not read(ROOT / group / "release.json").get("inverse_of")
    ):
        raise RuntimeError("no staged FB inverse")
    if group == "coordinator":
        marker = Path("/run/pipeline-social-media-rollback")
        with marker.open("x") as out:
            out.write(str(os.getpid()))
        try:

            def pending_finished():
                with sqlite3.connect(
                    Path(cfg["db_path"]).as_uri() + "?mode=ro", uri=True
                ) as c:
                    return (
                        c.execute(
                            "SELECT count(*) FROM pipeline_media_gate WHERE phase='pending'"
                        ).fetchone()[0]
                        == 0
                    )

            wait(
                pending_finished, 600, "media owners still acknowledging issued tickets"
            )
            switch(group, True)
        finally:
            marker.unlink(missing_ok=True)
        return
    if group in {"fb", "tt_auto", "x", "tag"}:
        key = "x" if group == "tag" else group
        paths = cfg["x_tag"]["paths"] if group == "tag" else cfg["paths"]
        tables = {
            "fb": [
                ("fb:auto:", "fb_auto_task", "id"),
                ("fb:video:", "fb_video_task", "task_id"),
            ],
            "tt_auto": [("tt:auto:", "tt_auto_task", "id")],
            "x": [("x:queue:", "x_post_queue", "id")],
        }[key]
        with sqlite3.connect(Path(paths[key]).as_uri() + "?mode=ro", uri=True) as c:
            if c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='pipeline_native_media_decision'"
            ).fetchone():
                for prefix, table, id_col in tables:
                    count = c.execute(
                        f"""SELECT count(*) FROM {table} t JOIN pipeline_native_media_decision d
                        ON d.source_key=?||t.{id_col} WHERE d.status='applied'
                        AND t.status NOT IN ('published','synced','completed','failed','failed_without_retry','cancelled','canceled','skipped')""",
                        (prefix,),
                    ).fetchone()[0]
                    if count:
                        raise RuntimeError(
                            f"{count} issued {group} decisions still need this owner; roll back coordinator first and retain this compatible reader"
                        )
    switch(group, not inverse)


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "stage":
        for group in sys.argv[2:] or GROUPS:
            stage(group)
    elif action == "stage-next":
        stage(sys.argv[2], sys.argv[3])
    elif action == "stage-inverse":
        stage(sys.argv[2], inverse_of=sys.argv[3])
    elif action == "apply-inverse":
        rollback(sys.argv[2], inverse=True)
    elif action == "refresh-timing":
        refresh_timing()
    elif action == "backup":
        backup()
    elif action == "mirror":
        mirror()
    elif action == "supply-original":
        supply_original(sys.argv[2])
    elif action == "warm":
        warm_start(*sys.argv[2:4])
    elif action == "activate":
        switch(sys.argv[2])
    elif action == "rollback":
        rollback(sys.argv[2])
    else:
        raise SystemExit("unsupported operation")
