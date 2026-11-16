"""Install the tested Pillow wheel files only in the new worker's package tree."""
import argparse
import base64
import hashlib
from importlib import metadata
import os
import json
import pwd
from pathlib import Path
import shutil
import subprocess
import tempfile


def provision(node):
    node=Path(node).resolve()
    if not (node/"features/pipeline_dispatch/cloud_node.py").is_file():
        raise ValueError("COS worker package required")
    provision_sdk_home()
    dist=metadata.distribution("Pillow")
    # This is the same interpreter and installed distribution used by the
    # accepted media fixtures. Shared Python environments are never modified.
    groups={"PIL","pillow.libs",str(dist.metadata["Name"]).lower()+"-"+dist.version+".dist-info"}
    with tempfile.TemporaryDirectory(dir=node) as tmp:
        staged=Path(tmp)
        for entry in dist.files or []:
            rel=Path(str(entry))
            if rel.parts[0] not in groups or ".." in rel.parts or "__pycache__" in rel.parts:continue
            origin=Path(dist.locate_file(entry))
            dest=staged/rel
            if entry.hash:
                digest=hashlib.new(entry.hash.mode,origin.read_bytes()).digest()
                if base64.urlsafe_b64encode(digest).decode().rstrip("=")!=entry.hash.value:
                    raise ValueError("installed Pillow file hash mismatch")
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(origin,dest)
            dest.chmod(0o644)
        # Make the shared library dependencies available before publishing PIL.
        for group in sorted(groups,key=lambda name:name=="PIL"):
            if not (node/group).exists():
                os.replace(staged/group,node/group)
    probe="from PIL import Image,ImageDraw,ImageFont; from PIL import _imaging; print(Image.__version__)"
    result=subprocess.check_output(["runuser","-u","pipeline-cos","--","/usr/bin/python3","-c",probe],
                                   cwd=node,text=True).strip()
    if result!=dist.version:raise RuntimeError("worker dependency verification failed")
    print("COS private Pillow",result,flush=True)


def provision_sdk_home():
    account=pwd.getpwnam("pipeline-cos")
    home=Path(account.pw_dir)
    target=Path("/mnt/data-disk/pipeline-cos-runtime/sdk-home")
    if home!=Path("/home/pipeline-cos") or not Path("/mnt/data-disk").is_mount():
        raise RuntimeError("unexpected COS service identity or data volume")
    target.mkdir(mode=0o700,exist_ok=True)
    os.chown(target,account.pw_uid,account.pw_gid)
    if not home.exists() and not home.is_symlink():
        home.symlink_to(target,target_is_directory=True)
    if home.resolve()!=target or target.stat().st_uid!=account.pw_uid:
        raise RuntimeError("COS SDK home belongs to another installation")
    subprocess.run(["runuser","-u","pipeline-cos","--","mkdir","-p",str(home/".cos_download_tmp_file")],check=True)


def tools_for_worker(node, cfg):
    bindir=Path(node)/"bin"
    bindir.mkdir(exist_ok=True)
    for name in ("ffmpeg","ffprobe"):
        original=Path(cfg[name])
        command=["runuser","-u","pipeline-cos","--"]
        if subprocess.run(command+[str(original),"-version"],capture_output=True).returncode:
            sha=hashlib.sha256(original.read_bytes()).hexdigest()
            binary=bindir/(name+"-"+sha[:16])
            if not binary.exists():
                fd,temp=tempfile.mkstemp(dir=bindir);os.close(fd)
                try:
                    shutil.copyfile(original,temp)
                    if hashlib.sha256(Path(temp).read_bytes()).hexdigest()!=sha:
                        raise RuntimeError(name+" copy failed verification")
                    os.chmod(temp,0o755);os.replace(temp,binary)
                finally:
                    if os.path.exists(temp):os.unlink(temp)
            cfg[name]=str(binary)
        subprocess.run(command+[cfg[name],"-version"],check=True,capture_output=True)
    return cfg


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("node",type=Path)
    parser.add_argument("--config",type=Path)
    args=parser.parse_args()
    provision(args.node)
    if args.config:
        stat=args.config.stat()
        cfg=tools_for_worker(args.node,json.loads(args.config.read_text()))
        fd,temp=tempfile.mkstemp(dir=args.config.parent)
        try:
            with os.fdopen(fd,"w") as out:json.dump(cfg,out)
            os.chown(temp,stat.st_uid,stat.st_gid);os.chmod(temp,stat.st_mode & 0o777)
            os.replace(temp,args.config)
        finally:
            if os.path.exists(temp):os.unlink(temp)
