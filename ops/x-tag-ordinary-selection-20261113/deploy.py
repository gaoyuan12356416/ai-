import json,sqlite3,hashlib,shutil,subprocess,os,time,urllib.request
from pathlib import Path
root=Path('/mnt/data-disk/x-tag-selection-fix-20261113');src=root/'source';link=Path('/opt/x-tag-publisher/current');old=link.resolve();f=Path('features/x_tag_publish/selection.py')
def run(*a):return subprocess.check_output(a,text=True).strip()
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
assert run('systemctl','show','x-tag-auto-runner.service','-p','ActiveState','--value')=='inactive'
assert sha(old/f)=='b7fa328db6af090edaefb743db260d148d84cb93dc72d9482d28733478c23fa7'
commit=run('git','-C',str(src),'rev-parse','HEAD');assert commit==run('git','-C',str(src),'rev-parse','origin/codex/x-tag-ordinary-selection-null-20261113')
assert not run('git','-C',str(src),'status','--porcelain')
assert (src/f).read_text()==(old/f).read_text().replace("context.get('hit', {}).get('timing_policy')","(context.get('hit') or {}).get('timing_policy')")
release=Path('/mnt/data-disk/x-tag-publisher/releases')/(commit[:12]+'-ordinary-selection')
shutil.copytree(old,release,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'));shutil.copy2(src/f,release/f)
compile((release/f).read_text(),str(release/f),'exec')
with sqlite3.connect('file:/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3?mode=ro',uri=True) as c:
 assert c.execute("select count(*) from x_auto_task where claim_worker<>''").fetchone()[0]==0
 with sqlite3.connect(root/'auto.before.sqlite3') as dest:c.backup(dest)
manifest=dict(commit=commit,previous=str(old),release=str(release),before_sha=sha(old/f),after_sha=sha(release/f))
(root/'deployment.json').write_text(json.dumps(manifest,indent=2))
# Production candidate inherited readable source modes; no secrets are copied.
for parent in [root,release]:parent.chmod(0o755)
nextlink=link.with_name('current.ordinary-selection-next');nextlink.symlink_to(release);os.replace(nextlink,link)
subprocess.run(['systemctl','restart','x-tag-auto.service'],check=True)
for _ in range(40):
 try:
  with urllib.request.urlopen('http://127.0.0.1:18843/health',timeout=2) as response:
   assert json.load(response)['ok'];break
 except OSError:time.sleep(.5)
else:raise RuntimeError('auto_service_not_ready')
subprocess.run(['systemctl','start','x-tag-auto-runner.timer','x-tag-auto-runner.path'],check=True)
subprocess.run(['systemctl','start','--no-block','x-tag-auto-runner.service'],check=True)
print(json.dumps(manifest))
