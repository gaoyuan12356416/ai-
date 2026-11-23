"""Reload spawn workers through their existing finish-current-claim SIGTERM path.

The supervisor stays alive, with its capacity and leases unchanged. Only its
verified multiprocessing spawn children receive the graceful drain request.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess


def main():
    p=argparse.ArgumentParser();p.add_argument('unit',choices=['pipeline-node-cpu-main.service','pipeline-node-hk-gpu.service']);p.add_argument('--evidence',required=True);a=p.parse_args()
    parent=int(subprocess.check_output(['systemctl','show',a.unit,'-p','MainPID','--value']))
    if parent<=1:raise RuntimeError('node_supervisor_not_running')
    children=[]
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try:
            stat=(entry/'stat').read_text().split(') ',1)[1].split()
            cmd=(entry/'cmdline').read_bytes()
            if int(stat[1])==parent and b'multiprocessing.spawn' in cmd and b'spawn_main' in cmd:
                children.append((int(entry.name),stat[19]))
        except FileNotFoundError:continue
    if not children:raise RuntimeError('no_spawn_workers_found')
    result={'unit':a.unit,'supervisor':parent,'workers':children,'signal':'SIGTERM','behavior':'finish current claim then exit; supervisor spawns replacement'}
    target=Path(a.evidence)
    with target.open('x') as f:json.dump(result,f,indent=2)
    for pid,started in children:
        try:
            stat=Path('/proc/%d/stat'%pid).read_text().split(') ',1)[1].split()
            if int(stat[1])!=parent or stat[19]!=started:continue
            os.kill(pid,signal.SIGTERM)
        except ProcessLookupError:pass
        except FileNotFoundError:pass
    print(json.dumps(result))


if __name__=='__main__':main()
