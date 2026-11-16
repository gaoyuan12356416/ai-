"""Hash-verified FB-only cache patch and natural preparer replacement.

No service, scheduler, capacity or publisher is stopped. SIGTERM is used only
with the verified worker's finish-current-claim handler and registered preparers.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

ENTRY = 'features/fb_gpu/prepare_worker.py'
SUPPORT = 'features/fb_gpu/source_cache.py'
IMPORT = 'from .source_cache import SourceCache, SourceDownloadError, observation as source_cache_observation\n'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    fd, temp = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def method(text, name):
    tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name=='PrepareProcessor')
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name==name)


def patched(before, source):
    old, new = before.decode(), source.decode()
    if 'from .source_cache import' in old:
        raise ValueError('cache_already_integrated')
    original, replacement = method(old, '_download'), method(new, '_download')
    old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
    old_lines[original.lineno-1:original.end_lineno] = new_lines[replacement.lineno-1:replacement.end_lineno]
    result = ''.join(old_lines)
    anchor = 'from .render_process import RenderStalled, run_render\n'
    if result.count(anchor)!=1:
        raise ValueError('import_anchor_changed')
    result = result.replace(anchor,anchor+IMPORT,1)
    anchor = '                if shutil.disk_usage(self.config.work_root).free<32*1024**3:'
    if result.count(anchor)!=1:
        raise ValueError('disk_reserve_anchor_changed')
    result = result.replace(anchor,
        '                SourceCache(self.config.work_root/"source-cache-v1").trim_if_low_disk(check=getattr(self,"execution_check",lambda:None))\n'+anchor,1)
    anchor = '"render_seconds":render_seconds},sort_keys=True'
    if result.count(anchor)!=1:
        raise ValueError('manifest_anchor_changed')
    result = result.replace(anchor,'"render_seconds":render_seconds,"source_cache":source_cache_observation(root)},sort_keys=True',1)
    compile(result,ENTRY,'exec')
    return result.encode()


def verified_source(root):
    head = subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
    if subprocess.check_output(['git','-C',str(root),'status','--porcelain']).strip():
        raise ValueError('source_worktree_not_clean')
    remote=subprocess.check_output(['git','-C',str(root),'remote','get-url','origin'],text=True).strip()
    if remote not in ('https://github.com/gaoyuan12356416/ai-.git','git@github.com:gaoyuan12356416/ai-.git'):
        raise ValueError('source_not_github_repository')
    refs = subprocess.check_output(['git','-C',str(root),'branch','-r','--contains',head]).strip()
    if not refs:
        raise ValueError('source_not_fetched_from_github')
    return head


def plan(source, runtime, expected):
    commit = verified_source(source)
    target = runtime/ENTRY
    if target.is_symlink():
        raise ValueError('entry_is_symlink')
    before = target.read_bytes()
    if sha(before)!=expected:
        raise ValueError('runtime_entry_changed')
    after = patched(before,(source/ENTRY).read_bytes())
    support = (source/SUPPORT).read_bytes()
    if (runtime/SUPPORT).exists() and (runtime/SUPPORT).read_bytes()!=support:
        raise ValueError('support_module_changed')
    return dict(commit=commit,runtime=str(runtime),source=str(source),
        before_sha256=sha(before),after_sha256=sha(after),support_sha256=sha(support)),before,after,support


def recycle(runtime, workers_file, unit):
    records = json.loads(workers_file.read_text())
    parent = int(subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True))
    worker_source = (runtime/'features/pipeline_dispatch/worker.py').read_text()
    if 'signal.signal(sig, lambda *_: stopping.set())' not in worker_source:
        raise ValueError('natural_worker_drain_unverified')
    if parent<=1:
        raise ValueError('node_parent_not_running')
    signaled = []
    for item in records:
        if item['stage']!='prepare':
            raise ValueError('nonpreparer_in_activation_scope')
        pid = int(item['pid'])
        try:
            proc = Path('/proc')/str(pid)
            if int((proc/'stat').read_text().split()[3])!=parent:
                continue
            if Path(os.readlink(proc/'cwd')).resolve()!=runtime.resolve():
                raise ValueError('worker_runtime_changed')
            if pid in (os.getpid(),parent) or pid<=1:
                raise ValueError('unsafe_preparer_pid')
            os.kill(pid,signal.SIGTERM)
            signaled.append(pid)
        except (ProcessLookupError,FileNotFoundError):
            continue
    return signaled


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action',choices=['plan','apply','rollback','recycle'])
    p.add_argument('--source',type=Path)
    p.add_argument('--runtime',type=Path)
    p.add_argument('--backup',type=Path)
    p.add_argument('--expected-before')
    p.add_argument('--workers-json',type=Path)
    p.add_argument('--unit')
    a = p.parse_args()
    if a.action=='recycle':
        print(json.dumps(dict(naturally_draining_prepare_pids=recycle(a.runtime,a.workers_json,a.unit))))
        return
    if a.action=='rollback':
        record = json.loads((a.backup/'manifest.json').read_text())
        target = Path(record['runtime'])/ENTRY
        current=sha(target.read_bytes())
        if current not in (record['after_sha256'],record['before_sha256']):
            raise ValueError('runtime_changed_after_cache_deployment')
        before = (a.backup/'prepare_worker.py').read_bytes()
        if sha(before)!=record['before_sha256']:
            raise ValueError('backup_hash_mismatch')
        if current!=record['before_sha256']:
            atomic(target,before)
        record.update(status='rolled_back_for_new_preparers',rollback_utc=datetime.now(timezone.utc).isoformat())
        (a.backup/'manifest.json').write_text(json.dumps(record,indent=2))
        print(json.dumps(record))
        return
    record,before,after,support=plan(a.source,a.runtime,a.expected_before)
    if a.action=='apply':
        a.backup.mkdir(mode=0o700,parents=True,exist_ok=False)
        (a.backup/'prepare_worker.py').write_bytes(before)
        record.update(status='backed_up',created_at_utc=datetime.now(timezone.utc).isoformat())
        (a.backup/'manifest.json').write_text(json.dumps(record,indent=2))
        atomic(a.runtime/SUPPORT,support)
        atomic(a.runtime/ENTRY,after)
        record['status']='installed_for_new_preparers'
        (a.backup/'manifest.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))


if __name__=='__main__':
    main()
