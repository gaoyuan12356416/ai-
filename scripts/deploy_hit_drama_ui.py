"""Static-only dialog spacing update, with both existing deployment manifests."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from scripts.deploy_hit_drama import atomic,sha,save,run

ROOT=Path('/mnt/data-disk/hit-drama-20261111')
asset=SOURCE/'static/hit-drama-publish.js'
commit=run('git','-C',SOURCE,'rev-parse','HEAD')
assert run('git','-C',SOURCE,'branch','-r','--contains',commit)
old=subprocess.check_output(['git','-C',str(SOURCE),'show','HEAD^:static/hit-drama-publish.js'])
before=hashlib.sha256(old).hexdigest();after=sha(asset)
folder=ROOT/'ui-spacing';folder.mkdir(exist_ok=False)
changes={}
roots=[Path('/opt/x-tag-publisher/current/static'),Path('/opt/fb-auto-post/current/static'),Path('/root/drama_material_service/static'),Path('/usr/share/nginx/html')]
for root in roots:
    assert sha(root/asset.name)==before,'shared_asset_changed'
    changes[str(root/asset.name)]=asset.read_bytes()
    for name in ('x-tag-auto-publish-templates.html','fb-auto-publish-templates.html'):
        path=root/name
        if path.exists() and '/hit-drama-publish.js?' in path.read_text():
            changes[str(path)]=re.sub(r'(/hit-drama-publish\.js)\?[^"\']+',r'\1?v='+after[:12],path.read_text()).encode()
info={'commit':commit,'files':{}}
for i,(name,data) in enumerate(changes.items()):
    backup=folder/str(i);backup.write_bytes(Path(name).read_bytes())
    info['files'][name]=dict(before=sha(name),after=hashlib.sha256(data).hexdigest(),backup=str(backup))
save(folder/'manifest.json',info)
for name,data in changes.items():atomic(name,data)
for platform in ('x','fb'):
    path=ROOT/(platform+'-deployment')/'manifest.json';record=json.loads(path.read_text())
    for name,row in record['static'].items():
        if name in changes:
            row['after']=sha(name);Path(row['next']).write_bytes(changes[name])
    record['ui_commit']=commit;save(path,record)
assert all(sha(name)==item['after'] for name,item in info['files'].items())
print(json.dumps(dict(status='active',commit=commit,static_files=len(changes),services_restarted=False)))
