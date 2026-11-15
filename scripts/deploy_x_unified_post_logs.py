"""GitHub-first four-file overlay; restart only the admin web API."""
import hashlib,json,os,shutil,subprocess,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FILES=['features/x_posts/log_query.py','features/x_posts/log_management.py','static/x-post-logs.html','static/x-post-logs.js']

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def install(folder):
    folder=Path(folder);folder.mkdir(mode=0o700,parents=True,exist_ok=False)
    revision=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    remote=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','origin/codex/x-unified-post-logs-20261115'],text=True).strip()
    assert revision==remote and not subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain'],text=True).strip()
    files=[]
    for f in FILES:
        targets=[Path('/root/drama_material_service')/f]
        if f.startswith('static/'):targets.append(Path('/usr/share/nginx/html')/Path(f).name)
        for dest in targets:
            backup=folder/('before-'+str(len(files)));shutil.copy2(dest,backup)
            files.append(dict(source=str(ROOT/f),target=str(dest),backup=str(backup),before=digest(dest),after=digest(ROOT/f)))
    (folder/'deployment.json').write_text(json.dumps(dict(revision=revision,files=files),indent=2))
    for v in files:
        dest=Path(v['target']);assert digest(dest)==v['before']
        tmp=dest.with_name(dest.name+'.unified-new');shutil.copy2(v['source'],tmp)
        stat=dest.stat();os.chown(tmp,stat.st_uid,stat.st_gid);os.chmod(tmp,stat.st_mode & 0o777);tmp.replace(dest)
    subprocess.check_call(['python3','-m','py_compile',*[str(Path('/root/drama_material_service')/f) for f in FILES if f.endswith('.py')]])
    subprocess.check_call(['node','--check','/root/drama_material_service/static/x-post-logs.js'])
    subprocess.check_call(['systemctl','restart','drama-material-api.service'])
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/ui/topbar',timeout=2) as response:
                if response.status==200:break
        except Exception:time.sleep(1)
    else:raise RuntimeError('admin_api_health_failed; use manifest code rollback')
    assert all(digest(v['target'])==v['after'] for v in files)
    print(json.dumps(dict(revision=revision,status='deployed',manifest=str(folder/'deployment.json'))))

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--directory',required=True);a=p.parse_args();install(a.directory)
