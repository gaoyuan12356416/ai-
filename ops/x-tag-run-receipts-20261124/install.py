"""Back up and install only the specified code, static assets and receipt observer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE=Path(__file__).resolve().parent
SRC=HERE.parents[1]
LIVE=Path("/opt/x-tag-publisher/current")
PUBLIC=[Path("/root/drama_material_service/static"),Path("/usr/share/nginx/html")]
ROOT=Path("/mnt/data-disk/x-tag-run-receipts-20261124")
BACKUP=ROOT/"backup/files"
PLAN=BACKUP/"manifest.json"


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def atomic(target,source,meta):
    target=Path(target)
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as tmp:
        temp=Path(tmp.name)
        try:
            tmp.write(Path(source).read_bytes())
            tmp.flush();os.fsync(tmp.fileno())
            os.fchown(tmp.fileno(),meta["uid"],meta["gid"]);os.fchmod(tmp.fileno(),meta["mode"])
        except BaseException:
            temp.unlink(missing_ok=True);raise
    os.replace(temp,target)


def prepare():
    if PLAN.exists():return json.loads(PLAN.read_text())
    BACKUP.mkdir(mode=0o700,parents=True,exist_ok=True)
    items={}
    def add(target,source,expected,phase):
        resolved=target.resolve()
        if str(resolved) in items:return
        if sha(resolved)!=expected:raise ValueError("deployed source changed: "+str(target))
        st=resolved.stat() if expected else None
        row=dict(path=str(resolved),alias=str(target),before=expected,source=str(source),after=sha(source),phase=phase,
                 uid=st.st_uid if st else 0,gid=st.st_gid if st else 0,mode=st.st_mode&0o777 if st else 0o644)
        if not row["after"]:raise ValueError("release file missing: "+str(source))
        if expected:
            backup=BACKUP/(str(len(items))+".before");shutil.copy2(resolved,backup);row["backup"]=str(backup)
        items[str(resolved)]=row
    for rec in json.loads((HERE/"files.json").read_text()):
        rel=Path(rec["file"]);is_web=rel.parts[0]=="static"
        add(LIVE/rel,SRC/rel,rec["before"],"web" if is_web else "code")
        if is_web:
            public_source=HERE/"public"/rel.name if rel.suffix==".html" else SRC/rel
            for root in PUBLIC:add(root/rel.name,public_source,rec["public_before"],"web")
    for ext in ("service","timer"):
        name="x-tag-receipt-settle."+ext
        add(Path("/etc/systemd/system")/name,SRC/"deploy"/name,None,"code")
    plan=list(items.values())
    PLAN.write_text(json.dumps(plan,indent=2));return plan


def switch(phase):
    plan=prepare()
    selected=[r for r in plan if r["phase"]==phase]
    selected.sort(key=lambda r:r["path"].endswith(".html"))
    for r in selected:
        if str(Path(r["alias"]).resolve())!=r["path"] or sha(Path(r["path"])) not in (r["before"],r["after"]):
            raise ValueError("concurrent change: "+r["alias"])
        if sha(Path(r["source"]))!=r["after"]: raise ValueError("staged release changed")
    for r in selected:
        if sha(Path(r["path"]))!=r["after"]:atomic(r["path"],r["source"],r)
    if not all(sha(Path(r["path"]))==r["after"] for r in selected):raise ValueError("readback failed")
    print(phase,len(selected))


def rollback():
    plan=json.loads(PLAN.read_text())
    for r in plan:
        if str(Path(r["alias"]).resolve())!=r["path"] or sha(Path(r["path"])) not in [r["after"],r["before"],*r.get("intermediate",[])]:
            raise ValueError("later change; review "+r["path"])
    subprocess.run(["systemctl","disable","--now","x-tag-receipt-settle.timer"],check=False)
    subprocess.run(["systemctl","stop","x-tag-receipt-settle.service"],check=False)
    for r in reversed(plan):
        p=Path(r["path"])
        if sha(p)==r["before"]:continue
        if r["before"]:atomic(p,r["backup"],r)
        else:p.unlink(missing_ok=True)
    subprocess.run(["systemctl","daemon-reload"],check=True)
    print("Restored code and web assets. Activate both X Tag processes in their next safe publishing gap.")


def revise_summary():
    plan=prepare()
    targets={str(LIVE/"features/x_auto_posts"/name) for name in ("run_outcomes.py","service.py")}
    selected=[r for r in plan if r["alias"] in targets]
    if len(selected)!=len(targets):raise ValueError("summary module manifest does not match")
    for r in selected:
        updated=sha(Path(r["source"]))
        current=sha(Path(r["path"]))
        if not updated or str(Path(r["alias"]).resolve())!=r["path"] or current not in [r["after"],*r.get("intermediate",[])]:
            raise ValueError("concurrent change: "+r["alias"])
        if r["after"]!=updated:
            r.setdefault("intermediate",[]).append(r["after"])
            r["after"]=updated
    # Preserve the original backups for a full rollback, including an interrupted switch.
    temp=PLAN.with_suffix(".new")
    temp.write_text(json.dumps(plan,indent=2))
    os.replace(temp,PLAN)
    for r in selected:
        if sha(Path(r["path"]))!=r["after"]:atomic(r["path"],r["source"],r)
        if sha(Path(r["path"]))!=r["after"]:raise ValueError("summary readback failed")


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("phase",choices=("prepare","code","web","rollback","revise-summary"))
    phase=parser.parse_args().phase
    if phase=="prepare":print(len(prepare()))
    elif phase=="rollback":rollback()
    elif phase=="revise-summary":revise_summary()
    else:switch(phase)
