"""Deploy the four-day Facebook source windows after an exact GitHub fetch."""
import hashlib,json,os,shutil,subprocess,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
REL='features/social_analytics/backend_scope.py'
target=Path('/root/drama_material_service')/REL
backup=Path('/mnt/data-disk/social-report-repair-20261029/deploy-backup-followup')
p=argparse.ArgumentParser();p.add_argument('--commit',required=True);args=p.parse_args()
commit=subprocess.check_output(['git','-C',str(ROOT),'rev-parse',args.commit],text=True).strip()
assert commit==subprocess.check_output(['git','-C',str(ROOT),'rev-parse','FETCH_HEAD'],text=True).strip()
before=subprocess.check_output(['git','-C',str(ROOT),'show','eef6d41:'+REL])
assert target.read_bytes()==before,'Concurrent live modification; stop'
data=subprocess.check_output(['git','-C',str(ROOT),'show',commit+':'+REL])
backup.mkdir(exist_ok=False)
saved=backup/'backend_scope.py';shutil.copy2(target,saved)
mode=target.stat().st_mode&0o777
(backup/'deployment.json').write_text(json.dumps(dict(commit=commit,path=str(target),backup=str(saved),old_mode=mode,old_sha256=hashlib.sha256(before).hexdigest()),indent=2))
tmp=target.with_name(target.name+'.report-next');tmp.write_bytes(data);tmp.chmod(mode);os.replace(tmp,target)
subprocess.run(['python3','-m','py_compile',str(target)],check=True)
print(json.dumps(dict(commit=commit,backup=str(backup))))
