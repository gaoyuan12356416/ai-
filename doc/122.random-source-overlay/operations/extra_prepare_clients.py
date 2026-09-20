"""Temporarily hold the two observed prepare-only operator loops, retaining requests."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

BASE=Path('/mnt/data-disk/random-overlay-gpu/backups/20260920-source-overlay')
JOURNAL=BASE/'extra-prepare-clients.json'

def save(value):
    tmp=JOURNAL.with_suffix('.new')
    with tmp.open('w') as f:json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())
    tmp.chmod(0o600);os.replace(str(tmp),str(JOURNAL))

def identity(pid):
    p=Path('/proc')/str(pid)
    s=(p/'stat').read_text().rsplit(')',1)[1].split()
    return p,s

if sys.argv[1]=='pause':
    assert not JOURNAL.exists()
    rows=[]
    for pid,marker in [(3558700,"gpu.prepare"),(3561822,"r.preparer.gpu.prepare")]:
        p,s=identity(pid);args=(p/'cmdline').read_bytes().split(b'\0')
        assert args[1:]==[b'-',b''] and s[0] not in ['T','t']
        code=(p/'fd/0').read_bytes()
        assert marker.encode() in code and b'stop-requested' in code
        assert not any(x in code for x in [b'.publisher.publish(',b'.publish_video(',b'upload_and_publish('])
        rows.append({'pid':pid,'start_ticks':s[19],'code_sha256':hashlib.sha256(code).hexdigest(),'pause_requested':False})
    save(rows)
    for row in rows:
        p,s=identity(row['pid']);assert s[19]==row['start_ticks']
        row['pause_requested']=True;save(rows)
        os.kill(row['pid'],signal.SIGSTOP)
        time.sleep(.1)
        assert identity(row['pid'])[1][0] in ['T','t']
        row['paused']=True;save(rows)
    print(json.dumps({'paused_prepare_clients':[r['pid'] for r in rows]}))
elif sys.argv[1]=='resume':
    rows=json.loads(JOURNAL.read_text())
    for row in rows:
        if row.get('resumed'):continue
        if Path('/proc/'+str(row['pid'])).exists():
            _,s=identity(row['pid']);assert s[19]==row['start_ticks']
            if row['pause_requested']:os.kill(row['pid'],signal.SIGCONT)
            row['resumed']=True
        else:row['resumed']='already_exited'
        save(rows)
    print(json.dumps({'resumed_prepare_clients':[r['pid'] for r in rows]}))
else:raise SystemExit('pause or resume required')
