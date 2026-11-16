"""Install the tested Pillow wheel files only in the new worker's package tree."""
import argparse
import base64
import hashlib
from importlib import metadata
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def provision(node):
    node=Path(node).resolve()
    if not (node/"features/pipeline_dispatch/cloud_node.py").is_file():
        raise ValueError("COS worker package required")
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


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("node",type=Path)
    provision(parser.parse_args().node)
