"""Build an isolated candidate from the reviewed GitHub revision and active owner packages."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

BASE = "752a77d11e0fdd579b213a0e687f84593b192ab1"
LIVE = Path("/opt/pipeline-dispatch/current")
DATA = Path("/mnt/data-disk")
ROOT = DATA / "pipeline-cos-runtime"
NATIVE = [
    ("fb","fb-auto-post-service",18835,18941,"fb","FB_AUTO_POST_INTERNAL_TOKEN"),
    ("x","x-post-automation",8810,18942,"x","PIPELINE_X_CANONICAL_TOKEN"),
    ("x-tag","x-tag-post",8821,18943,"x","PIPELINE_X_CANONICAL_TOKEN"),
    ("tt","tt-post-service",18829,18944,"tt","TT_POST_INTERNAL_TOKEN"),
    ("tt-auto","tt-auto-post-service",18831,18945,"tt","TT_AUTO_POST_INTERNAL_TOKEN")]
LIVE_FILES = {"core.py":"6926f24236fe3596","service.py":"151d3f0240a914cf",
              "adapters.py":"143501e75fa44e0f","views.py":"392a595622641b71","monitor.py":"b020529d766ac4e6"}
STATIC = {"pipeline-monitor.html":"53545f389a60b942", "pipeline-monitor.js":"b38812c7fc67a1db"}
COORDINATOR = "pipeline-coordinator.service"
PYTHON = "/opt/pipeline-dispatch/coordinator-venv/bin/python"
HEALTH = "/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py"


def run(argv, **kwargs):
    return subprocess.check_output([str(arg) for arg in argv], text=True, **kwargs).strip()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owner(unit):
    pid = int(run(["systemctl","show",unit,"-p","MainPID","--value"]))
    if pid <= 1:
        raise RuntimeError("owner offline: " + unit)
    return (Path("/proc") / str(pid) / "cwd").resolve()


def write(path, data, mode=0o644):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as out:
            out.write(data if isinstance(data,bytes) else data.encode())
        os.chmod(tmp,mode)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def put_json(path, data):
    write(path,json.dumps(data,indent=2),0o600)


def changes(package, mirror, sha, active, name, dest):
    rel = "features/pipeline_dispatch/" + name
    if name == "fb_native.py":
        if file_hash(active / rel)[:16] != "d8b9a4e832a7d304":
            raise RuntimeError("FB owner revision changed")
        shutil.copy2(package / rel, dest / name)
        return
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder) / "base"
        base.write_bytes(subprocess.check_output(["git","--git-dir="+str(mirror),"show",BASE+":"+rel]))
        result=subprocess.run(["git","merge-file","-p",str(active/rel),str(base),str(package/rel)],capture_output=True)
        if result.returncode:
            raise RuntimeError("native compatibility conflict: " + name)
        write(dest / name, result.stdout)


def build(package, mirror, settings):
    sha = run(["git","--git-dir="+str(mirror),"rev-parse","refs/cos-dispatch/validation"])
    if package.name != sha or not DATA.is_mount() or owner(COORDINATOR) != LIVE.resolve():
        raise RuntimeError("GitHub revision, mount or active coordinator differs")
    active = LIVE.resolve()
    static_dirs = [Path("/usr/share/nginx/html"),Path("/root/drama_material_service/static")]
    for name, prefix in LIVE_FILES.items():
        if not file_hash(active/"features/pipeline_dispatch"/name).startswith(prefix):
            raise RuntimeError("coordinator revision changed: " + name)
    for folder in static_dirs:
        for name, prefix in STATIC.items():
            if not file_hash(folder/name).startswith(prefix):
                raise RuntimeError("monitor revision changed: " + str(folder/name))
    release = ROOT / "versions" / sha
    release.mkdir(parents=True,exist_ok=True)
    candidate, node = release/"coordinator", release/"node"
    shutil.copytree(active,candidate,symlinks=True,dirs_exist_ok=True,ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    dispatch = Path("features/pipeline_dispatch")
    for name in [*LIVE_FILES,"prepare_routing.py","cloud_admin.py"]:
        shutil.copy2(package/dispatch/name,candidate/dispatch/name)
    for name in STATIC:
        shutil.copy2(package/"static"/name,candidate/"static"/name)
    shutil.copytree(package/"features",node/"features",dirs_exist_ok=True)
    scripts=node/"scripts"
    scripts.mkdir(exist_ok=True)
    for name in ["pipeline_cos_native.py","pipeline_cos_credentials.py","pipeline_cos_launch.py",
                 "pipeline_cos_control.py","pipeline_cos_handoff.py","pipeline_cos_prepare_release.py","pipeline_cos_dependencies.py"]:
        shutil.copy2(package/"scripts"/name,scripts/name)
    ports = {str(port):alternate for _,_,port,alternate,_,_ in NATIVE}
    configdir=release/"launch"
    configdir.mkdir(exist_ok=True,mode=0o700)
    configs=[]
    for ident, unit, port, alternate, channel, token_env in NATIVE:
        origin=owner(unit)
        overlay=release/"overlays"/ident
        overlay.mkdir(parents=True,exist_ok=True)
        for name in ([channel+"_native.py","tt_profiles.py"] if channel=="tt" else [channel+"_native.py"]):
            changes(package,mirror,sha,origin,name,overlay)
        bridge={"runtime":str(origin),"overlay":str(overlay),"port":alternate,"channel":channel,"automatic":ident=="tt-auto",
                "tagged":ident=="x-tag","token_env":token_env,"prefix":"/internal/pipeline-dispatch/"+channel}
        cfg=configdir/(ident+".json"); put_json(cfg,bridge)
        launch=configdir/(ident+"-launch.json")
        put_json(launch,{"runtime":str(origin),"owner":unit+".service","argv":["/usr/bin/python3",str(scripts/"pipeline_cos_native.py"),"--config",str(cfg)]})
        configs.append((ident,launch))
    put_json(configdir/"standby.json",{"runtime":str(candidate),"owner":COORDINATOR,
        "environment":{"PIPELINE_COS_NATIVE_PORTS":json.dumps(ports,separators=(",",":"))},
        "argv":[PYTHON,"-m","features.x_tag_publish.dispatch","--config","/etc/pipeline-coordinator.json","--standby-port","18940"]})
    backup=release/"backup";backup.mkdir(exist_ok=True,mode=0o700)
    for i,folder in enumerate(static_dirs):
        for name in STATIC:shutil.copy2(folder/name,backup/(str(i)+"-"+name))
    shutil.copy2("/etc/pipeline-coordinator.json",backup/"pipeline-coordinator.json")
    for name,path in [("original-unit.txt",COORDINATOR)]:
        write(backup/name,run(["systemctl","cat",path]),0o600)
    # No shell/unit credential values are logged or copied into the node tree.
    cfg=json.loads(settings.read_text())
    for field in ["canary_url","passthrough_url","fixture_version"]:
        cfg.pop(field,None)
    cfg.update(prefix="pipeline-cloud-prepare/v1",delivery_prefix="pipeline-cloud-prepared/v1",
        state_root=str(ROOT/"state"),node_config="/etc/pipeline-cos-cloud/node.json",
        credential_file="/etc/pipeline-cos-cloud/ci-session.json")
    put_json(configdir/"worker-base.json",cfg)
    manifest={"revision":sha,"previous":str(active),"candidate":str(candidate),"node":str(node),"ports":ports,
              "native":[ident for ident,_ in configs],"static_dirs":[str(d) for d in static_dirs],
              "live_checksums":{n:file_hash(active/dispatch/n) for n in LIVE_FILES},
              "old_static":{n:file_hash(static_dirs[0]/n) for n in STATIC}}
    put_json(release/"release.json",manifest)
    print("candidate built",sha,"bridges",len(configs))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    for name in ("package","mirror","settings"):parser.add_argument(name,type=Path)
    opts=parser.parse_args()
    build(opts.package,opts.mirror,opts.settings)
