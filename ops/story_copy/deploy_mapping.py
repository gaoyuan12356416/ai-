"""Overlay metadata lookup and batch alerts onto the current FB/central variants."""
import argparse
import ast
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SRC))
from ops.story_copy import deploy as d
from ops.story_copy.deploy_click_hooks import central_switch

CATALOG = 'features/fb_video_publish/sources.py'
COPY_FILES = ['features/story_copy/' + n for n in ('sources.py', 'worker.py', 'alerts.py')]
BASE = '3dea0145'


def method(text):
    cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == 'SourceRepository')
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_enrich')
    return node, ''.join(text.splitlines(True)[node.lineno-1:node.end_lineno])


def overlay(text):
    baseline = subprocess.check_output(['git', '-C', str(SRC), 'show', BASE + ':' + CATALOG]).decode('utf-8')
    node, old = method(text)
    assert old.strip() == method(baseline)[1].strip(), 'metadata method changed concurrently'
    lines = text.splitlines(True)
    lines[node.lineno-1:node.end_lineno] = [method((SRC / CATALOG).read_text(encoding='utf-8'))[1]]
    result = ''.join(lines).encode()
    compile(result, CATALOG, 'exec')
    return result


def prepare(folder):
    assert d.read('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 1024**3
    assert not d.read('git', '-C', SRC, 'status', '--porcelain')
    folder.mkdir(mode=0o755, parents=True, exist_ok=False)
    rec = dict(commit=d.read('git','-C',SRC,'rev-parse','HEAD'), central_before=str(d.CENTRAL.resolve()), roots={}, files={})
    for lane, origin, names in [('central', d.CENTRAL.resolve(), COPY_FILES), ('fb', d.ROOTS['fb'].resolve(), [CATALOG])]:
        candidate = folder / lane
        shutil.copytree(origin, candidate, symlinks=True, copy_function=os.link,
                        ignore=shutil.ignore_patterns('.git','__pycache__','*.pyc'))
        files = {}
        for name in names:
            before = (origin/name).read_bytes()
            if lane == 'central':
                expected = subprocess.check_output(['git','-C',str(SRC),'show',BASE+':'+name])
                assert before == expected, 'central module changed concurrently: '+name
                after = (SRC/name).read_bytes()
            else:
                after = overlay(before.decode())
            compile(after,name,'exec')
            d.atomic(candidate/name,after)
            files[name] = dict(before=d.sha(before),after=d.sha(after))
        rec['roots'][lane] = dict(before_root=str(origin),candidate=str(candidate),files=files)
    d.stage_file(folder,rec,d.MAIN/CATALOG,overlay((d.MAIN/CATALOG).read_text()))
    for name,path in [('fb',d.store.SOURCES['fv'][1]),('captions',d.store.DB)]:
        with contextlib.closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)) as src:
            with contextlib.closing(sqlite3.connect(folder/(name+'-before.sqlite3'))) as dst:
                src.backup(dst)
        (folder/(name+'-before.sqlite3')).chmod(0o600)
    d.save(folder,rec)
    print(json.dumps({'prepared':str(folder),'commit':rec['commit']}),flush=True)


def activate(folder, reverse=False):
    rec=json.loads((folder/'record.json').read_text())
    d.check_files(rec,reverse)
    old,new=Path(rec['central_before']),folder/'central'
    assert d.CENTRAL.resolve() in (old,new), 'later central release'
    if d.sched(folder,'snapshot','fb',reverse):
        raise RuntimeError('FB scheduler snapshot failed')
    try:
        d.switch_lane(folder,rec,'fb',reverse)
        d.install_files(folder,rec,reverse)
        central_switch(new,old) if reverse else central_switch(old,new)
    finally:
        if d.sched(folder,'restore','fb',reverse):
            raise RuntimeError('FB scheduler restoration failed')
    rec['state']='rolled_back' if reverse else 'active'
    d.save(folder,rec)
    print(json.dumps({'state':rec['state'],'commit':rec['commit']}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('prepare','activate','rollback'))
    p.add_argument('folder',type=Path)
    args=p.parse_args()
    folder=args.folder.resolve()
    assert folder.parent==Path('/mnt/data-disk'), 'unexpected deployment path'
    prepare(folder) if args.action=='prepare' else activate(folder,args.action=='rollback')
