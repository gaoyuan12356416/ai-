"""Stage a shared-logic follow-up while retaining the active worker and native bridges."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from pipeline_cos_prepare_release import ROOT, LIVE, COORDINATOR, file_hash, owner, put_json, run, write


def build(package,mirror):
    sha=run(["git","--git-dir="+str(mirror),"rev-parse","refs/cos-dispatch/validation"])
    active=LIVE.resolve()
    if owner(COORDINATOR)!=active or package.name!=sha:raise RuntimeError("expected current owner and GitHub revision")
    prev=active.parent;mf=json.loads((prev/"release.json").read_text())
    if active!=Path(mf["candidate"]):raise RuntimeError("unexpected current release")
    target=ROOT/"versions"/sha;candidate=target/"coordinator"
    target.mkdir(parents=True,exist_ok=True)
    backup=target/"backup";backup.mkdir(exist_ok=True,mode=0o700)
    dispatch=Path("features/pipeline_dispatch")
    changed=run(["git","--git-dir="+str(mirror),"diff","--name-only",mf["revision"],sha,"--",
                 *[str(dispatch/name) for name in ["service.py","core.py","adapters.py","views.py","monitor.py","prepare_routing.py","cloud_admin.py"]]]).splitlines()
    if not changed:raise RuntimeError("no shared code changes")
    if not candidate.exists():shutil.copytree(active,candidate,symlinks=True,ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    checks={}
    for rel in changed:
        rel=Path(rel)
        baseline=subprocess.check_output(["git","--git-dir="+str(mirror),"show",mf["revision"]+":"+rel.as_posix()])
        if (active/rel).read_bytes()!=baseline:raise RuntimeError("concurrent change to "+str(rel))
        checks[rel.name]=file_hash(active/rel)
        shutil.copy2(active/rel,backup/rel.name)
        shutil.copy2(package/rel,candidate/rel)
    standby=json.loads((prev/"launch/standby.json").read_text());standby["runtime"]=str(candidate)
    put_json(target/"launch/standby.json",standby)
    mf.update(revision=sha,previous=str(active),candidate=str(candidate),live_checksums=checks)
    put_json(target/"release.json",mf)
    shutil.copy2("/etc/pipeline-coordinator.json",backup/"pipeline-coordinator.json")
    write(backup/"original-unit.txt",run(["systemctl","cat",COORDINATOR]),0o600)
    print("incremental candidate",sha,[Path(n).name for n in changed],flush=True)


if __name__=="__main__":
    build(Path(sys.argv[1]),Path(sys.argv[2]))
