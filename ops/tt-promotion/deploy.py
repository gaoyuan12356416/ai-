"""Stage a scoped GitHub release; record every mutable target and preserve publishing facts."""
import argparse
import ast
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile

SRC = Path(__file__).resolve().parents[2]
OPS = Path(__file__).resolve().parent
DATA = Path("/mnt/data-disk/tt-promotion")
DEPLOY = Path("/mnt/data-disk/tt-promotion-releases")
CURRENT = Path("/opt/tt-promotion/current")
MAIN = Path("/root/drama_material_service")
PUBLIC = Path("/usr/share/nginx/html")
ETC = Path("/etc/systemd/system")
TT = Path("/opt/tt-auto-post/current")
WAREHOUSE = Path("/opt/social-analytics-warehouse/current")
STORY = Path("/opt/story-copy/current")
REPORTS = {"warehouse": WAREHOUSE, "daily":Path("/opt/youtube-publisher-daily-report/current"),
           "gap":Path("/opt/socialkit-analytics/current"), "post":Path("/opt/post-daily-report/current")}
UNITS = ("tt-promotion.service","tt-promotion-run.timer","tt-promotion-copy.timer","tt-promotion-profile.timer")
EFFECTS = Path("features/social_analytics/tiktok.py")
DAILY = Path("features/post_daily_report/tt.py")


def run(*cmd,**kw):
    return subprocess.run([str(x) for x in cmd],check=True,**kw)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(p):
    return p.read_bytes() if p.is_file() else None


def blob(p):
    return sha(p.read_bytes()) if p.is_file() else None


def atomic(path,data,mode=0o644):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as out:
        tmp = Path(out.name)
        out.write(data)
    try:
        tmp.chmod(mode)
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)


def point(link,dest):
    link.parent.mkdir(parents=True,exist_ok=True)
    alt=link.with_name(link.name+".tt-promotion-next")
    alt.unlink(missing_ok=True)
    alt.symlink_to(dest,target_is_directory=True)
    os.replace(alt,link)


def json_bytes(obj):
    return (json.dumps(obj,ensure_ascii=False,indent=2)+"\n").encode()


def replace_once(text,old,new):
    if text.count(old)!=1:
        raise ValueError("live source anchor differs: "+old[:96])
    return text.replace(old,new,1)


def allow_modules(src):
    text=src.decode()
    for old,extra in [
      ('    "tt_posts": "TikTok 社媒发布",','    "tt_promotion_channels": "TT 频道管理（新版）",\n    "tt_promotion_auto": "TT 自动发布（新版）",'),
      ('    "tt_posts": False,','    "tt_promotion_channels": False,\n    "tt_promotion_auto": False,')]:
        if extra not in text:
            text=replace_once(text,old,old+"\n"+extra)
    ast.parse(text)
    return text.encode()


def nav_items(raw):
    cfg=json.loads(raw)
    items=json.loads((OPS/"navigation.json").read_text())
    group,=[g for g in cfg if g.get("key")=="tiktok_platform"]
    inherited=group.pop("module",None)
    if inherited:
        for old in group["items"]:
            if old.get("module") not in (None,inherited):
                raise ValueError("inspect mixed child access before changing parent")
            old.setdefault("module",inherited)
    keys={i["key"] for i in items}
    group["items"]=[i for i in group["items"] if i["key"] not in keys]+items
    return json_bytes(cfg)


def write_manifest(folder,obj):
    atomic(folder/"manifest.json",json_bytes(obj),0o600)


def stage(sha_id):
    mount_uuid=run("findmnt","-n","-o","UUID","--target","/mnt/data-disk",stdout=subprocess.PIPE,text=True).stdout.strip()
    if mount_uuid!="3e8ac4e8-7770-456d-9e89-2ec5dd405fa8" or not Path("/mnt/data-disk").is_mount() or shutil.disk_usage("/mnt/data-disk").free < 256*1024**2:
        raise RuntimeError("mounted data disk required")
    folder=DEPLOY/sha_id
    folder.mkdir(parents=True,exist_ok=True)
    if (folder/"manifest.json").is_file():
        return folder
    manifest={"commit":sha_id,"files":[],"links":[],"units":list(UNITS),"new_units":{}}
    runtime=folder/"runtime"
    runtime.mkdir(exist_ok=True)
    fr=runtime/"features"
    fr.mkdir(exist_ok=True)
    (fr/"__init__.py").touch()
    for name in ("tt_promotion","x_tag_revenue","drama_audience"):
        shutil.copytree(SRC/"features"/name,fr/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns("__pycache__"))
    # The report and native publishers have different deployed composite builds.
    # Pin each required package to its owner. Copy public modules under /root
    # only when they are absent from the three service releases.
    for rel in (TT,WAREHOUSE,STORY):
        for package in (rel/"features").iterdir():
            if package.name.startswith("_") or (fr/package.name).exists():
                continue
            (fr/package.name).symlink_to(package.resolve(),target_is_directory=package.is_dir())
    for name in ("social_analytics","youtube_analytics"):
        p=fr/name
        if p.is_symlink():
            p.unlink()
        if not p.exists():
            p.symlink_to((WAREHOUSE/"features"/name).resolve())
    for name in ("vendor","scripts","ops"):
        if not (runtime/name).exists():
            (runtime/name).symlink_to((TT/name).resolve(),target_is_directory=True)
    profile_db=DATA/"profiles"
    DATA.mkdir(mode=0o750,exist_ok=True)
    acct=pwd.getpwnam("tt-post")
    os.chown(DATA,acct.pw_uid,acct.pw_gid)
    DATA.chmod(0o2750)
    profile_db.mkdir(mode=0o2750,exist_ok=True)
    os.chown(profile_db,0,acct.pw_gid)
    profile_db.chmod(0o2750)

    def edit(target,data):
        old=read(target)
        if old==data:
            return
        seq=len(manifest["files"])
        path=folder/"stage"/str(seq)
        atomic(path,data)
        if old is not None:
            atomic(folder/"before"/str(seq),old)
        manifest["files"].append({"target":str(target.resolve()),"index":seq,
               "before":sha(old) if old is not None else None,"after":sha(data)})

    for nm in OPS.glob("tt-promotion*"):
        if nm.suffix in (".service",".timer"):
            edit(ETC/nm.name,nm.read_bytes())
    edit(Path("/etc/nginx/default.d/tt-promotion.conf"),(OPS/"tt-promotion.conf").read_bytes())
    for htmlroot in (MAIN/"static",PUBLIC):
        for src in (SRC/"static").glob("tt-promotion*"):
            edit(htmlroot/src.name,src.read_bytes())
        edit(htmlroot/"navigation.json",nav_items((htmlroot/"navigation.json").read_bytes()))
    app=MAIN/"app.py"
    edit(app,allow_modules(app.read_bytes()))
    # Modify only the new TT source adapter; other live report overlays must survive.
    old_effects=run("git","-C",SRC,"show","477cce36:"+str(EFFECTS),stdout=subprocess.PIPE).stdout
    source_effects=(SRC/EFFECTS).read_bytes()
    current=(MAIN/EFFECTS).read_bytes()
    if current not in (old_effects,source_effects):
        raise ValueError("main TT attribution source changed")
    edit(MAIN/EFFECTS,source_effects)
    for name,link in REPORTS.items():
        base=link.resolve()
        dest=folder/("report-"+name)
        if not dest.exists():
            shutil.copytree(base,dest,symlinks=True,ignore=shutil.ignore_patterns("__pycache__",".git"))
        if name!="post":
            if read(base/EFFECTS) not in (old_effects,source_effects):
                raise ValueError("TT attribution live variant needs review: "+name)
            atomic(dest/EFFECTS,source_effects)
        else:
            base_source=(base/DAILY).read_text()
            # Use the exact integration from the GitHub source tree.
            new=(SRC/DAILY).read_text()
            anchor='    for row in output["rows"]:\n'
            loc=new.rindex(anchor)
            lines=new[:loc].splitlines(keepends=True)
            hook="".join(lines[-2:])
            if "tt_promotion" not in hook:
                raise ValueError("unexpected publication-report hook")
            if hook not in base_source:
                loc=base_source.rindex(anchor)
                base_source=base_source[:loc]+hook+base_source[loc:]
            ast.parse(base_source)
            atomic(dest/DAILY,base_source.encode())
            aux="features/post_daily_report/tt_promotion.py"
            atomic(dest/aux,(SRC/aux).read_bytes())
        manifest["links"].append({"link":str(link),"before":str(base),"after":str(dest)})
    manifest["links"].insert(0,{"link":str(CURRENT),"before":str(CURRENT.resolve()) if CURRENT.is_symlink() else None,"after":str(runtime)})
    for unit in UNITS:
        manifest["new_units"][unit]=subprocess.run(["systemctl","is-active","--quiet",unit]).returncode==0
    write_manifest(folder,manifest)
    print(folder)
    return folder


def init_db(folder):
    # Stable path owned by the new service, never the native publisher databases.
    runtime=folder/"runtime"
    for script in ["from features.tt_promotion.channels import initialize,promotion_db;initialize(promotion_db())",
                   "import sys,runpy;sys.argv=['publishing','init'];runpy.run_module('features.tt_promotion.publishing',run_name='__main__')"]:
        run("runuser","-u","tt-post","--","/usr/bin/python3","-B","-c",script,cwd=runtime)
    with sqlite3.connect((DATA/"promotion.sqlite3").as_uri()+"?mode=ro",uri=True) as db:
        for view in ("tt_promotion_report_plan","tt_promotion_report_task"):
            db.execute("select * from "+view+" limit 0")
    (DATA/"promotion.sqlite3").chmod(0o660)


def file_switch(rec,folder,rollback=False):
    target=Path(rec["target"])
    key,dest=("after","before") if rollback else ("before","after")
    actual=blob(target)
    if actual==rec[dest]:
        return
    if actual!=rec[key]:
        raise RuntimeError("later change preserved; review "+str(target))
    if rec[dest] is None:
        target.unlink()
    else:
        path=folder/("before" if rollback else "stage")/str(rec["index"])
        if blob(path)!=rec[dest]:
            raise RuntimeError("staged checksum mismatch")
        atomic(target,path.read_bytes(),target.stat().st_mode & 0o777 if target.exists() else 0o644)


def switch_link(item,rollback=False):
    link=Path(item["link"])
    expected,dest=(item["after"],item["before"]) if rollback else (item["before"],item["after"])
    actual=str(link.resolve()) if link.is_symlink() else None
    if actual==dest:
        return
    if actual!=expected:
        raise RuntimeError("concurrent release changed "+str(link))
    if dest:
        point(link,dest)
    else:
        link.unlink(missing_ok=True)


def has_facts():
    path=DATA/"promotion.sqlite3"
    if not path.exists():
        return False
    with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as db:
        return bool(db.execute("select count(*) from tt_promotion_report_plan").fetchone()[0])


def switch(folder,rollback):
    manifest=json.loads((folder/"manifest.json").read_text())
    files,links=manifest["files"],manifest["links"]
    preserve=rollback and has_facts()
    worker_was_active=subprocess.run(["systemctl","is-active","--quiet","drama-youtube-publish-worker.service"]).returncode==0
    if not rollback:
        init_db(folder)
    if rollback:
        for u in UNITS:
            if not manifest["new_units"][u]:
                subprocess.run(["systemctl","disable","--now",u],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for unit in ("tt-promotion-run.service","tt-promotion-copy.service"):
            if subprocess.run(["systemctl","is-active","--quiet",unit]).returncode==0:
                raise RuntimeError("in-flight independent worker: wait for it to finish before rollback")
    for rec in (reversed(files) if rollback else files):
        if preserve and rec["target"].endswith(str(EFFECTS)):
            continue
        file_switch(rec,folder,rollback)
    for item in (reversed(links) if rollback else links):
        if preserve and item["link"]!=str(CURRENT):
            continue
        switch_link(item,rollback)
    run("nginx","-t")
    run("systemctl","daemon-reload")
    if any(r["target"]==str((MAIN/"app.py").resolve()) for r in files):
        run("systemctl","restart","drama-material-api.service")
        if worker_was_active:
            run("systemctl","start","drama-youtube-publish-worker.service")
    if not rollback:
        run("systemctl","enable","--now",*UNITS)
        run("systemctl","restart","tt-promotion.service")
    run("systemctl","reload","nginx")
    print("rolled back" if rollback else "activated",manifest["commit"],"preserved report ingress" if preserve else "")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('action',choices=['stage','init','activate','rollback'])
    ap.add_argument('commit')
    args=ap.parse_args()
    sha_id=run("git","-C",SRC,"rev-parse","HEAD",stdout=subprocess.PIPE,text=True).stdout.strip()
    if len(args.commit)!=40 or sha_id!=args.commit:
        raise ValueError("deployment source commit mismatch")
    folder=DEPLOY/sha_id
    if args.action=="stage":
        stage(sha_id)
        return
    with open(folder/"switch.lock","a") as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        if args.action=="init":
            init_db(folder)
        else:
            switch(folder,args.action=="rollback")


if __name__=="__main__":
    main()
