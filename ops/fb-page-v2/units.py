"""Stage or roll back only this V2 release's pointer and systemd/nginx files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

POINTER = Path("/opt/fb-page-assignment-v2/current")
UNIT_DIR = Path("/etc/systemd/system")
CPU_UNITS = {"catalog-export.service": "fb-page-v2-catalog-export.service",
             "catalog-export.timer": "fb-page-v2-catalog-export.timer",
             "inventory.service": "fb-page-v2-inventory.service",
             "inventory.timer": "fb-page-v2-inventory.timer",
             "alerts.service": "fb-page-v2-alerts.service"}
HK_UNITS = {"hk.service": "fb-page-v2.service",
            "catalog-pull.service": "fb-page-v2-catalog-pull.service",
            "catalog-pull.timer": "fb-page-v2-catalog-pull.timer"}
NGINX = Path("/etc/nginx/default.d/fb-page-v2-alerts.conf")


def run(*args, check=True):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def signature(data):
    return hashlib.sha256(data).hexdigest()


def install(path, payload, mode=0o644):
    temp = path.with_name("." + path.name + "." + str(os.getpid()))
    with temp.open("xb") as f:
        os.fchmod(f.fileno(), mode)
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(temp), str(path))


def link(target):
    POINTER.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = POINTER.with_name(".current." + str(os.getpid()))
    tmp.symlink_to(target)
    os.replace(str(tmp), str(POINTER))


def files(role):
    units = CPU_UNITS if role == "cpu" else HK_UNITS
    pairs = {v: (k, UNIT_DIR / v) for k, v in units.items()}
    if role == "cpu":
        pairs["nginx"] = ("alerts.nginx.conf", NGINX)
    return pairs


def stage(role, release, backup):
    release = release.resolve()
    backup.mkdir(exist_ok=True, parents=True, mode=0o700)
    cfg = backup / "units.json"
    if cfg.exists():
        raise ValueError("backup_already_taken")
    facts = {"release": str(release), "role": role,
             "previous": os.readlink(str(POINTER)) if POINTER.is_symlink() else None, "files": {}}
    if POINTER.exists() and not POINTER.is_symlink():
        raise RuntimeError("unexpected_release_pointer")
    payloads = {}
    for name, (src, dest) in files(role).items():
        data = (release/"ops/fb-page-v2"/src).read_bytes()
        payloads[name] = data
        previous = dest.is_file()
        if previous:
            install(backup / name, dest.read_bytes(), 0o600)
        facts["files"][name] = {"sha256": signature(data), "previous": previous,
            "enabled": name != "nginx" and run("systemctl","is-enabled","--quiet",name,check=False).returncode == 0,
            "active": name != "nginx" and run("systemctl","is-active","--quiet",name,check=False).returncode == 0}
    install(cfg, json.dumps(facts,sort_keys=True,indent=2).encode(), 0o600)
    link(str(release))
    for name, (_, dest) in files(role).items():
        install(dest, payloads[name])
    run("systemctl","daemon-reload")
    if role == "cpu":
        run("nginx","-t")
        run("systemctl","reload","nginx")


def rollback(backup):
    facts = json.loads((backup / "units.json").read_text())
    role = facts["role"]
    if POINTER.is_symlink() and os.readlink(str(POINTER)) not in (facts["release"],facts["previous"]):
        raise RuntimeError("newer_release_present")
    ordered = sorted(files(role).items(), key=lambda pair: not pair[0].endswith(".timer"))
    for name, (_, dest) in ordered:
        f = facts["files"][name]
        if dest.exists() and signature(dest.read_bytes()) != f["sha256"]:
            if not f["previous"] or dest.read_bytes() != (backup/name).read_bytes():
                raise RuntimeError("changed_file_" + name)
    for name, (_, dest) in ordered:
        f = facts["files"][name]
        if name != "nginx" and dest.exists():
            if not f["active"]:
                run("systemctl","stop",name)
            if not f["enabled"]:
                run("systemctl","disable",name,check=False)
        if f["previous"]:
            install(dest,(backup/name).read_bytes())
        elif dest.exists():
            dest.unlink()
    if facts["previous"]:
        link(facts["previous"])
    elif POINTER.is_symlink():
        POINTER.unlink()
    run("systemctl","daemon-reload")
    for name, f in facts["files"].items():
        if f["enabled"]:
            run("systemctl","enable",name)
        if f["active"]:
            run("systemctl","restart",name)
    if role == "cpu":
        run("nginx","-t")
        run("systemctl","reload","nginx")


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("action", choices=("stage","rollback"))
    cli.add_argument("role", choices=("cpu","hk"))
    cli.add_argument("--backup", required=True, type=Path)
    args = cli.parse_args()
    if args.action == "stage":
        stage(args.role, Path(__file__).resolve().parents[2], args.backup)
    else:
        rollback(args.backup)
