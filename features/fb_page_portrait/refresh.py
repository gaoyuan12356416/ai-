"""Independent bounded refresh; a failed source preserves the last good version."""
import argparse
from datetime import datetime,timezone
import fcntl
import json
from pathlib import Path
from .build import build
from .collect import collect,environment,Reader
from .runtime import atomic_json,encoded
from .storage import ROOT,write,publish
from .page_list import refresh_names
from features.fb_page_current.refresh import primary,repair_handoff,secondary


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',default=ROOT);p.add_argument('--env',default='/root/drama_material_service/.env')
    p.add_argument('--create-table',action='store_true');p.add_argument('--resume-input',type=Path);args=p.parse_args()
    root=Path(args.root);root.mkdir(parents=True,exist_ok=True)
    with (root/'refresh.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            initial_env=environment(args.env)
            repair_handoff(Reader(initial_env),initial_env,root)
            if args.resume_input:
                from .resume import restore
                raw,data=restore(root,args.resume_input);env=environment(args.env);reader=Reader(env)
                print(encoded(dict(event='resume_original_version',version=data['meta']['version'])),flush=True)
            else:
                raw,env,reader=collect(args.env)
                try:
                    count=refresh_names(reader,raw['page_ids'],root)
                    print(encoded(dict(event='page_names_refreshed',rows=count)),flush=True)
                except Exception as exc:
                    # A display name refresh must not invalidate financial data.
                    print(encoded(dict(event='page_names_preserved',error=type(exc).__name__)),flush=True)
                data=build(raw)
                atomic_json(root/(data['meta']['version']+'.input.json'),raw)
            proof=primary(data,env,reader,create_table=args.create_table)
            atomic_json(root/(data['meta']['version']+'.receipt.json'),proof)
            publish(data,proof,root)
            try:
                from .partition_cache import complete
                complete(raw)
            except Exception as exc:
                print(encoded(dict(event='partition_cache_maintenance_failed',error=type(exc).__name__)),flush=True)
            atomic_json(root/'status.json',dict(status='ok',**data['meta']))
            secondary(data,env,reader,proof)
            # The verified DB version and small receipt are sufficient after
            # success; retain failed inputs for the existing resume workflow.
            (root/(data['meta']['version']+'.input.json')).unlink(missing_ok=True)
            import resource
            print(encoded(dict(event='profile_memory_peak',max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)),flush=True)
            print(encoded(dict(event='published',**data['meta'])),flush=True)
        except Exception as exc:
            atomic_json(root/'status.json',dict(status='error',at=datetime.now(timezone.utc).isoformat(),error=type(exc).__name__))
            raise


if __name__=='__main__':main()
