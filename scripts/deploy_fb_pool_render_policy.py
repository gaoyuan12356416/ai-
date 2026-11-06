"""Install exact GitHub render-policy diff and gracefully refresh prepare workers."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy_publish_queue_isolation import copy_file,run,save,sha,handlers,pid
BASE='f44b3b47dda1fff4d2dae2910ad81a6554f74f50'
REL='features/fb_gpu/prepare_worker.py'


def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--folder',type=Path,required=True)
    p.add_argument('--unit',required=True);p.add_argument('--workers',nargs='*',type=int);p.add_argument('--http-port',type=int)
    a=p.parse_args();source=Path(__file__).resolve().parents[1];runtime=a.runtime.resolve();folder=a.folder.resolve()
    assert any(folder.is_relative_to(Path(root)) for root in ('/data','/mnt/data-disk'))
    commit=run(['git','rev-parse','HEAD'],cwd=source)
    assert run(['git','ls-remote','origin','refs/heads/codex/fb-page-pool-any-product-20261106'],cwd=source).split()[0]==commit
    assert Path(run(['systemctl','show',a.unit,'-p','WorkingDirectory','--value'])).resolve()==runtime
    parent=pid(a.unit);assert int(parent)>1
    children=[]
    for worker in a.workers or []:
        stat=Path('/proc',str(worker),'stat')
        if not stat.exists():continue
        assert int(stat.read_text().split(') ',1)[1].split()[1])==int(parent),'worker_parent_changed'
        assert b'multiprocessing.spawn' in Path('/proc',str(worker),'cmdline').read_bytes()
        children.append(worker)
    if not a.http_port:assert children,'no_verified_prepare_workers'
    folder.mkdir(parents=True,exist_ok=False)
    backup=folder/'before.py';shutil.copy2(runtime/REL,backup)
    target=folder/'candidate'/REL;target.parent.mkdir(parents=True);shutil.copy2(runtime/REL,target)
    patch=run(['git','diff',BASE,commit,'--',REL],cwd=source)+'\n'
    run(['git','apply','--check','-'],cwd=folder/'candidate',input=patch)
    run(['git','apply','-'],cwd=folder/'candidate',input=patch)
    compile(target.read_bytes(),REL,'exec')
    manifest=dict(commit=commit,runtime=str(runtime),unit=a.unit,parent_pid=parent,prepare_workers=children,
                  before=sha(backup),after=sha(target),backup=str(backup))
    save(folder/'manifest.json',manifest)
    assert sha(runtime/REL)==manifest['before'];copy_file(target,runtime/REL)
    if a.http_port:
        # Only the fallback FB prepare HTTP process; no active render is killed.
        marker='fb-pool-render-'+str(int(time.time()))
        rule=['-p','tcp','--dport',str(a.http_port),'-m','conntrack','--ctstate','NEW','-m','comment','--comment',marker,'-j','DROP']
        run(['systemd-run','--quiet','--unit='+marker,'--on-active=60s','--','/usr/sbin/iptables','-D','INPUT']+rule)
        run(['iptables','-I','INPUT','1']+rule)
        try:
            until=time.monotonic()+25
            while handlers(parent)!=[1,0,0]:
                if time.monotonic()>until:raise RuntimeError('fallback_render_still_active_no_restart')
                time.sleep(.25)
            run(['systemctl','restart',a.unit],timeout=20)
            manifest['active_pid']=pid(a.unit)
        finally:
            run(['iptables','-D','INPUT']+rule)
            run(['systemctl','stop',marker+'.timer'])
    else:
        # process_worker handles TERM by finishing its claim and flushing receipts.
        # run_node respawns prepare children; publishing children are untouched.
        for worker in children:
            if Path('/proc',str(worker)).exists():os.kill(worker,signal.SIGTERM)
    manifest['installed']=True;save(folder/'manifest.json',manifest)
    print(json.dumps(manifest),flush=True)


if __name__=='__main__':main()
