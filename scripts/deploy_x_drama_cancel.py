"""Overlay only the committed cancellation patch onto the current X composite."""
import argparse,json,shutil,subprocess
from pathlib import Path
from datetime import datetime,timezone
import deploy_x_pool_local_failures as base

# This scheduler became a long-running producer; stop/restart it as well as its timer.
base.TRIGGERS = list(dict.fromkeys(base.TRIGGERS + ['x-video-pool-scheduler.service']))

FILES=['features/x_posts/service.py','static/x-post-drama-pool.html']
EXPECTED={'features/x_posts/service.py':'07baee9ea23a9770d238f03bcf8dddc2d60ab537390789185394545806bfba3c',
          'static/x-post-drama-pool.html':'11e3e8be1f4c8a06c4c69d4956b36d2c56226c3bb9ca325e234a9f8bc3084e9f'}

def apply_patch(target,rel,commit):
    patch=subprocess.check_output(['git','-C',str(base.SOURCE),'diff',commit+'^',commit,'--',rel])
    assert patch
    subprocess.run(['git','apply','--check','-'],cwd=target,input=patch,check=True,capture_output=True)
    subprocess.run(['git','apply','-'],cwd=target,input=patch,check=True,capture_output=True)

def prepare():
    assert not base.run('git','-C',str(base.SOURCE),'status','--porcelain')
    commit=base.run('git','-C',str(base.SOURCE),'rev-parse','HEAD')
    assert base.run('git','-C',str(base.SOURCE),'branch','-r','--contains',commit)
    old=base.CURRENT.resolve()
    for rel in FILES:assert base.digest(old/rel)==EXPECTED[rel],rel
    tag=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-drama-cancel-'+commit[:8]
    folder,release=base.ROOT/'backups'/tag,base.ROOT/'releases'/tag
    folder.mkdir(mode=0o700)
    before=base.tree(old)
    shutil.copytree(old,release,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for rel in FILES:apply_patch(release,rel,commit)
    test='scripts/test_x_post_multi_schedule_store.py';shutil.copy2(base.SOURCE/test,release/test)
    compile((release/FILES[0]).read_bytes(),FILES[0],'exec')
    external=[]
    # Patch a scratch copy of the main service; preserve all unrelated overlays.
    scratch=folder/'main-candidate';(scratch/'features/x_posts').mkdir(parents=True)
    shutil.copy2(base.MAIN/FILES[0],scratch/FILES[0]);apply_patch(scratch,FILES[0],commit)
    for target,candidate in [(base.MAIN/FILES[0],scratch/FILES[0]),
            (base.MAIN/FILES[1],release/FILES[1]),(Path('/usr/share/nginx/html/x-post-drama-pool.html'),release/FILES[1])]:
        n=len(external);saved=folder/('before-'+str(n));nxt=folder/('next-'+str(n))
        shutil.copy2(target,saved);shutil.copy2(candidate,nxt)
        external.append(dict(path=str(target),before=str(saved),after=str(nxt),before_sha=base.digest(saved),after_sha=base.digest(nxt)))
    assert base.tree(old)==before
    m=dict(phase='prepared',commit=commit,source=str(base.SOURCE),old_release=str(old),new_release=str(release),old_tree=before,new_tree=base.tree(release),external=external)
    base.save(folder,m);print(json.dumps(dict(folder=str(folder),release=str(release),commit=commit)))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['prepare','drain','switch','abort','rollback']);p.add_argument('--folder',type=Path);a=p.parse_args()
    if a.phase=='prepare':prepare()
    else:
        f=a.folder.resolve();assert f.parent==base.ROOT/'backups' and '-drama-cancel-' in f.name
        m=json.loads((f/'manifest.json').read_text())
        if a.phase in ['drain','rollback']:r=base.drain(f,m,a.phase=='rollback')
        elif a.phase=='switch':r=base.activate(f,m)
        else:
            assert m['phase']=='draining';base.resume(f,m);m['phase']='prepared';base.save(f,m);r={'phase':'prepared','admissions_restored':True}
        print(json.dumps(r))
