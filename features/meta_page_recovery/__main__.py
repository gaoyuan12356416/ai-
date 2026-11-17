"""CLI used by the isolated scanner and worker systemd units."""
import argparse
import json
import os
from pathlib import Path
import time
import signal
import threading

from .domain import encoded
from .sources import environment,Sources,shared
from .state import Store


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['scan','work','status','preview','notify'])
    p.add_argument('--root',default='/mnt/data-disk/meta-page-recovery')
    p.add_argument('--env',default='/etc/meta-page-recovery.env')
    p.add_argument('--once',action='store_true');p.add_argument('--acceptance',action='store_true')
    a=p.parse_args();env=environment(a.env);shared(env)
    if a.command=='preview':
        pages=Sources(env).pages()
        print(encoded({'groups':{str(g):{'pages':sum(x['group_id']==g for x in pages),'abnormal':sum(x['group_id']==g and (x.get('status')==1 or x.get('forbidden_status')==1) for x in pages)} for g in (24,25,57)},'mutation_calls':0}));return
    os.umask(0o077)
    root=Path(a.root);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if a.command=='status':
        if not (root/'state.sqlite3').exists():print(encoded({'status':'not_initialized'}));return
        print(encoded(Store(root/'state.sqlite3').snapshot()));return
    import fcntl
    with (root/(a.command+'.lock')).open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        store=Store(root/'state.sqlite3')
        if a.command=='scan':
            from .engine import scan
            scan(store,Sources(env));return
        from .notify import Sender
        sender=Sender(env)
        if a.command=='notify':sender.deliver(store,a.acceptance);return
        from features.fb_page_assignment.sources import load_profiles
        from .engine import Worker,log
        store.recover_process()
        stop=threading.Event()
        signal.signal(signal.SIGTERM,lambda *_:stop.set())
        signal.signal(signal.SIGINT,lambda *_:stop.set())
        while not stop.is_set():
            try:
                worker=Worker(store,Sources(env),env,load_profiles,stop=stop)
                worker.tick()
            except Exception as exc:
                log('worker_iteration_failed',error_type=type(exc).__name__)
            try:
                sender.deliver(store)
            except Exception as exc:
                log('notification_iteration_failed',error_type=type(exc).__name__)
            if a.once:break
            stop.wait(15)


if __name__=='__main__':main()
