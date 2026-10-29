"""Deploy the four-day Facebook source windows after an exact GitHub fetch."""
import hashlib,json,os,shutil,subprocess,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
RELS=['features/social_analytics/backend_scope.py','features/social_analytics/routes.py']
backup=Path('/mnt/data-disk/social-report-repair-20261029/deploy-backup-followup')
p=argparse.ArgumentParser();p.add_argument('--commit',required=True);args=p.parse_args()
commit=subprocess.check_output(['git','-C',str(ROOT),'rev-parse',args.commit],text=True).strip()
assert commit==subprocess.check_output(['git','-C',str(ROOT),'rev-parse','FETCH_HEAD'],text=True).strip()
files=[]
for rel in RELS:
    target=Path('/root/drama_material_service')/rel
    before=subprocess.check_output(['git','-C',str(ROOT),'show','eef6d41:'+rel])
    assert target.read_bytes()==before,'Concurrent live modification; stop'
    data=subprocess.check_output(['git','-C',str(ROOT),'show',commit+':'+rel])
    files.append((target,before,data))
backup.mkdir(exist_ok=False)
manifest={'commit':commit,'files':[]}
for target,before,data in files:
    saved=backup/target.name;shutil.copy2(target,saved)
    manifest['files'].append(dict(path=str(target),backup=str(saved),old_mode=target.stat().st_mode&0o777,old_sha256=hashlib.sha256(before).hexdigest()))
(backup/'deployment.json').write_text(json.dumps(manifest,indent=2))
for (target,before,data),item in zip(files,manifest['files']):
    tmp=target.with_name(target.name+'.report-next');tmp.write_bytes(data);tmp.chmod(item['old_mode']);os.replace(tmp,target)
    subprocess.run(['python3','-m','py_compile',str(target)],check=True)
print(json.dumps(dict(commit=commit,backup=str(backup))))
