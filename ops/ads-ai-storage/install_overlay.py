"""Apply the reviewed small patches to a mixed production runtime, without restarts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
PATCHES = {
    'app.py': 'app.patch',
    'features/drama_synthesis/gpu_cache.py': 'gpu-cache.patch',
    'features/drama_synthesis/material_source.py': 'material-source.patch',
    'features/pipeline_dispatch/cloud_node.py': 'cloud-node.patch',
    'scripts/pipeline_cos_credentials.py': 'cloud-credentials.patch',
}


def install(root, backup, names, apply=False):
    root = root.resolve(); backup.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(dir=backup) as folder:
        stage = Path(folder)
        for rel in names:
            path = root / rel
            if not path.is_file(): continue
            dest = stage / rel; dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            original = path.read_bytes()
            patch = HERE / PATCHES[rel]
            result = subprocess.run(['git','apply','--check',str(patch)],cwd=stage,capture_output=True)
            if result.returncode:
                reverse = subprocess.run(['git','apply','--reverse','--check',str(patch)],cwd=stage,capture_output=True)
                if reverse.returncode: raise RuntimeError('patch does not match '+str(path))
                rows.append({'path':str(path),'already_applied':True});continue
            subprocess.run(['git','apply',str(patch)],cwd=stage,check=True,capture_output=True)
            candidate = dest.read_bytes(); compile(candidate,str(path),'exec')
            rows.append({'path':str(path),'sha_before':hashlib.sha256(original).hexdigest(),'sha_after':hashlib.sha256(candidate).hexdigest()})
            if apply:
                assert path.read_bytes()==original, 'source changed during staging: '+str(path)
                saved=backup/rel;saved.parent.mkdir(parents=True,exist_ok=True)
                if not saved.exists():shutil.copy2(path,saved)
                temporary=path.with_name('.'+path.name+'.ads-ai-storage')
                shutil.copy2(dest,temporary);os.replace(temporary,path)
        if any('/drama_synthesis/' in '/'+n or n=='app.py' for n in names):
            rel='features/drama_synthesis/storage_targets.py'; dest=root/rel
            if (root/'features/drama_synthesis').is_dir():
                candidate=(SOURCE/rel).read_bytes();compile(candidate,str(dest),'exec')
                if apply:
                    saved=backup/rel;saved.parent.mkdir(parents=True,exist_ok=True)
                    if dest.exists() and not saved.exists():shutil.copy2(dest,saved)
                    temporary=dest.with_name('.'+dest.name+'.ads-ai-storage');temporary.write_bytes(candidate);temporary.chmod(0o644);os.replace(temporary,dest)
                rows.append({'path':str(dest),'new_module':True,'sha_after':hashlib.sha256(candidate).hexdigest()})
    (backup/('applied.json' if apply else 'preflight.json')).write_text(json.dumps(rows,indent=2))
    return rows


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--backup',type=Path,required=True)
    p.add_argument('--kind',choices=['drama','cloud'],required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    names=list(PATCHES)[:3] if a.kind=='drama' else list(PATCHES)[3:]
    print(json.dumps(install(a.root,a.backup,names,a.apply),indent=2))
