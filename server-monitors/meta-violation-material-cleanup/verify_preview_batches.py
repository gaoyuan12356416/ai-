#!/usr/bin/env python3
"""Read-only completion of a source preview; no schedule or live changes."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import signal
import sqlite3
import threading
import time

from run import load_config,validate_paths,validate_run_id,load_manifest,open_graph,summary
from meta_violation_cleanup.runtime import Runtime,process_lock
from meta_violation_cleanup.sql_source import SqlSource
from meta_violation_cleanup.runner import Runner
from meta_violation_cleanup.verify_batches import verify


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--source-run-id',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--wait',action='store_true')
    args=parser.parse_args();config=load_config(args.config);validate_paths(config)
    if config.get('live_enabled'):raise ValueError('verification_requires_read_only_config')
    source_id=validate_run_id(args.source_run_id);run_id=validate_run_id(args.run_id)
    if source_id==run_id:raise ValueError('verification_requires_new_artifact_identity')
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    root=Path(config['runtime_dir']);deadline=time.monotonic()+6*3600
    if args.wait:
        while not stop.is_set():
            with sqlite3.connect('file:'+str(root/'runs.sqlite3')+'?mode=ro',uri=True) as conn:
                row=conn.execute('SELECT status FROM runs WHERE id=?',(source_id,)).fetchone()
            if row and row[0]!='running':break
            if time.monotonic()>deadline:raise RuntimeError('preview_wait_timeout')
            stop.wait(15)
    if stop.is_set():raise InterruptedError('stop_requested')
    with process_lock(root/'process.lock'):
        runtime=Runtime(root);source=graph=None
        try:
            if runtime.get(source_id)['status']!='previewed':raise ValueError('source_preview_not_complete')
            base=load_manifest(runtime,source_id)
            baseline=runtime.load_artifact(base['comparison']['baseline_run_id'],'manifest.json')
            if not runtime.begin(run_id,'preview',{'window_start':base['window_start'],'window_end':base['window_end'],
                    'stage':'starting_verification','verified_from_run_id':source_id}):
                raise ValueError('verification_run_already_exists')
            try:
                source=SqlSource(config);graph=open_graph(config,source)
                runner=Runner(config,source,graph,runtime,None,None,stop.is_set)
                result=verify(runner,source_id,run_id,base,baseline)
                # The original freeze deadline also applies after all retries.
                load_manifest(runtime,run_id)
                runtime.update(run_id,status='previewed',stage='frozen',
                    graph_metrics=result['scan_graph_metrics'],sql_metrics=result['scan_sql_metrics'])
            except BaseException as exc:
                runtime.update(run_id,status='interrupted' if isinstance(exc,InterruptedError) else 'failed',
                    error_type=type(exc).__name__)
                raise
            print(json.dumps(summary(runtime,run_id),ensure_ascii=False),flush=True)
        finally:
            for resource in (graph,source,runtime):
                if resource is not None:resource.close()


if __name__=='__main__':main()
