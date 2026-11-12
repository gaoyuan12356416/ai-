"""Scoped installation from a verified GitHub checkout; never touches business data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BASELINE = {
    "app.py": "986384e3a712c97f9f4672c59cd1a669854135196eceaf07936790f5500c096b",
    "static/quick-nav.js": "97b22a40182ff57453c7428db45af3746fdb901595ca36dc90ee9f04f5877c76",
    "static/index.html": "6b3daf39411d20116a51911b94d3a0c146e79b13aef9bb2791e599329daa2693",
}
UNITS = ("moboreels-library.service", "moboreels-library-sync.service", "moboreels-library-sync.timer")
HEALTH = "/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py"


def run(*args, check=True):
    return subprocess.run(args, check=check, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent
    target = Path("/root/drama_material_service")
    public = Path("/usr/share/nginx/html")
    deployment = Path(args.deployment).resolve()
    if not str(deployment).startswith("/mnt/data-disk/moboreels-library/deployments/"):
        raise SystemExit("Deployment backup must stay on the data disk")
    if run("findmnt", "-n", "-o", "UUID", "/mnt/data-disk") != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise SystemExit("Data disk is unavailable")
    sha = run("git", "-C", str(source), "rev-parse", "HEAD")
    if run("git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("Source checkout is dirty")
    for name, expected in BASELINE.items():
        actual = hashlib.sha256((target / name).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit("Live baseline changed: " + name)
    deployment.mkdir(parents=True, exist_ok=False)
    os.chmod(str(deployment), 0o700)
    baseline = deployment / "schedulers-before.json"
    print(run(sys.executable, HEALTH, "snapshot", "--lane", "youtube", "--output", str(baseline)), flush=True)
    package = list((source / "features/moboreels_library").glob("*.py"))
    copies = [(source / name, target / name) for name in BASELINE]
    copies.extend((p, target / "features/moboreels_library" / p.name) for p in package)
    static = ["index.html", "quick-nav.js", "moboreels-library.html", "moboreels-library.js", "moboreels-library.css"]
    copies.extend((source / "static" / n, public / n) for n in static)
    copies.extend((source / "static" / n, target / "static" / n) for n in static[2:])
    copies.extend((source / "deploy" / n, Path("/etc/systemd/system") / n) for n in UNITS)
    records = []
    for index, (src, dst) in enumerate(copies):
        old = deployment / ("backup-%03d" % index)
        exists = dst.exists()
        if exists:
            shutil.copy2(str(dst), str(old))
        records.append({"target": str(dst), "backup": str(old) if exists else None,
                        "new_sha256": hashlib.sha256(src.read_bytes()).hexdigest()})
    current = Path("/opt/moboreels-library/current")
    previous = os.readlink(str(current)) if current.is_symlink() else None
    if current.exists() and not current.is_symlink():
        raise SystemExit("Unexpected non-symlink runtime path")
    manifest = {"commit": sha, "source": str(source), "files": records, "previous_current": previous}
    (deployment / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rollback = '''import json, pathlib, shutil, subprocess, os
p=pathlib.Path(__file__).resolve().parent
m=json.loads((p/'manifest.json').read_text())
subprocess.run(['systemctl','disable','--now','moboreels-library-sync.timer'],check=False)
subprocess.run(['systemctl','stop','moboreels-library.service'],check=False)
# Source rollback only: preserve MySQL, snapshots, SQLite, receipts and credentials.
for r in m['files']:
 d=pathlib.Path(r['target'])
 if r['backup']: shutil.copy2(r['backup'],str(d))
 elif d.exists():
  import hashlib
  if hashlib.sha256(d.read_bytes()).hexdigest()!=r['new_sha256']: raise SystemExit('Newer file exists: '+str(d))
  d.unlink()
c=pathlib.Path('/opt/moboreels-library/current')
if m['previous_current']:
 t=c.with_name('rollback-current');t.symlink_to(m['previous_current']);os.replace(str(t),str(c))
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','restart','drama-material-api.service'],check=True)
subprocess.run(['python3','/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py','restore','--baseline',str(p/'schedulers-before.json'),'--wait-seconds','25','--output',str(p/'schedulers-rollback.json')],check=True)
print('Source restored; business records and credentials retained')
'''
    (deployment / "rollback.py").write_text(rollback, encoding="utf-8")
    for src, dst in copies:
        dst.parent.mkdir(parents=True, exist_ok=True)
        temporary = dst.with_name(dst.name + ".moboreels-new")
        shutil.copy2(str(src), str(temporary))
        os.replace(str(temporary), str(dst))
    current.parent.mkdir(parents=True, exist_ok=True)
    link = current.with_name("next")
    link.symlink_to(source)
    os.replace(str(link), str(current))
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "moboreels-library.service")
    run("systemctl", "restart", "drama-material-api.service")
    print(run(sys.executable, HEALTH, "restore", "--baseline", str(baseline), "--wait-seconds", "25", "--output", str(deployment / "schedulers-after.json")), flush=True)
    run("systemctl", "enable", "--now", "moboreels-library-sync.timer")
    print(json.dumps({"commit": sha, "deployment": str(deployment), "rollback": "python3 " + str(deployment / "rollback.py")}))


if __name__ == "__main__":
    main()
