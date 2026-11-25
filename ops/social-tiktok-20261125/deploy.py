from pathlib import Path
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]
BASE='bb55a66f8627eafbe9419f90bbd68072a87e32ec'
ROOTS={'warehouse':Path('/opt/social-analytics-warehouse/current'),
       'daily':Path('/opt/youtube-publisher-daily-report/current'),
       'kit':Path('/opt/socialkit-analytics/current'),
       'web':Path('/root/drama_material_service'),
       'hk':Path('/data/ai-backend-offload/read-current')}
def digest(data):
    return hashlib.sha256(data).hexdigest()
def read(path):
    return path.read_bytes() if path.is_file() else None
def checked(path, value):
    data=read(path)
    if (digest(data) if data is not None else None)!=value:
        raise ValueError('unexpected deployed or staged file: '+str(path))
    return data
def replace(src,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as tmp:
        temp=Path(tmp.name)
    try:
        shutil.copy2(src,temp)
        temp.chmod(0o644)
        os.replace(temp,target)
    finally:
        if temp.exists(): temp.unlink()
def entries(role):
    return json.loads((HERE/'files.json').read_text())[role]
def stage(role,root):
    dest=root/role/'stage'
    for name,rec in entries(role).items():
        old=checked(ROOTS[role]/name,rec['before'])
        canonical=checked(REPO/name,rec['canonical'])
        if rec['after']==rec['canonical']:
            data=canonical
        else:
            # Apply the Git-reviewed change to the exact deployed runtime variant.
            with tempfile.TemporaryDirectory() as d:
                d=Path(d)
                (d/'module').write_bytes(old)
                subprocess.run(['git','apply','--whitespace=nowarn',str(HERE/'variants'/(rec['after']+'.patch'))],cwd=d,check=True)
                data=(d/'module').read_bytes()
        if digest(data)!=rec['after']:raise ValueError('variant checksum failed: '+name)
        file=dest/name
        file.parent.mkdir(parents=True,exist_ok=True)
        file.write_bytes(data)
        if file.suffix=='.py': compile(data,str(file),'exec')
    print('staged',role,len(entries(role)))
def release(role,root):
    pointer=ROOTS[role]
    if not pointer.is_symlink():raise ValueError('release pointer required')
    dest=root/role/'release'
    if dest.exists():raise ValueError('already staged '+str(dest))
    original=pointer.resolve()
    (root/role/'previous').write_text(str(original)+'\n')
    shutil.copytree(original,dest,symlinks=True,ignore=shutil.ignore_patterns('.git','__pycache__'))
    for name,rec in entries(role).items():
        checked(dest/name,rec['before'])
        source=root/role/'stage'/name
        checked(source,rec['after'])
        replace(source,dest/name)
    print('prepared release',role)
def point(role,root,rollback):
    pointer=ROOTS[role]
    prev=Path((root/role/'previous').read_text().strip()); dest=root/role/'release'
    before,after=(dest,prev) if rollback else (prev,dest)
    if pointer.resolve()==after:return
    if pointer.resolve()!=before or not after.is_dir():raise ValueError('release changed '+str(pointer))
    tmp=pointer.with_name('.'+pointer.name+'.social-tt-'+str(os.getpid()))
    tmp.symlink_to(after);os.replace(tmp,pointer)
    print('activated',role,after)
def web(root,rollback):
    files=[]
    for name,rec in entries('web').items():
        for label,target in [('api',ROOTS['web']/name)]+([('public',Path('/usr/share/nginx/html')/Path(name).name)] if name.startswith('static/') else []):
            before=root/'web'/'before'/label/name
            source=root/'web'/'stage'/name
            checked(source,rec['after'])
            current=read(target)
            value=digest(current) if current is not None else None
            expected=rec['after' if rollback else 'before']; desired=rec['before' if rollback else 'after']
            if value==desired:continue
            if value!=expected:raise ValueError('live changed: '+str(target))
            if not rollback and current is not None:
                if not before.exists():
                    before.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,before)
                checked(before,rec['before'])
            if rollback and rec['before'] is not None:checked(before,rec['before'])
            files.append((before if rollback else source,target,desired))
    for src,target,value in files:
        if value is None:target.unlink()
        else:replace(src,target)
        checked(target,value)
    print('restored' if rollback else 'installed','web',len(files))
if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=('stage','release','switch','rollback'))
    p.add_argument('role',choices=ROOTS);p.add_argument('directory',type=Path)
    a=p.parse_args();root=a.directory.resolve()
    if a.action=='stage':stage(a.role,root)
    elif a.action=='release':release(a.role,root)
    elif a.role=='web':web(root,a.action=='rollback')
    else:point(a.role,root,a.action=='rollback')
