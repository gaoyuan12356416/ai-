"""Small overlay onto active FB variant; no task replay or database changes."""
import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
from ops.story_copy import deploy as d

FILES = ('features/fb_auto_posts/repositories.py', 'features/fb_video_publish/sources.py')


def class_text(text):
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == 'PageTarget')
    lines = text.splitlines(keepends=True)
    return ''.join(lines[node.lineno - 1:node.end_lineno])


def overlay(name, text):
    if name == FILES[0]:
        old = class_text(text)
        new = class_text((SRC / name).read_text())
        expected = new.split('\n    def __post_init__')[0].rstrip()
        if old.rstrip() == new.rstrip():
            return text
        if old.rstrip() not in (expected, expected.replace('    page_name: str = ""', '').rstrip()):
            raise RuntimeError('PageTarget changed; preserve runtime variant')
        method = '\n    def __post_init__' + new.split('\n    def __post_init__', 1)[1]
        return text.replace(old, old.rstrip() + '\n' + method, 1)
    old = '"language": page_language(str(row.get("language") or "")[:32]),'
    new = '"language": page_language((str(row.get("language") or "").strip() or "en")[:32]),'
    if text.count(old) != 1:
        raise RuntimeError('Page list changed; preserve runtime variant')
    return text.replace(old, new, 1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('stage', 'apply', 'rollback'))
    p.add_argument('--commit', required=True)
    p.add_argument('--directory', required=True)
    args = p.parse_args()
    folder = Path(args.directory)
    if d.read('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data disk missing')
    if args.action == 'stage':
        if shutil.disk_usage('/mnt/data-disk').free < 1024**3:
            raise RuntimeError('insufficient disk space')
        folder.mkdir(parents=True, exist_ok=False, mode=0o755)
        origin = d.ROOTS['fb'].resolve()
        candidate = folder / 'fb-release'
        shutil.copytree(origin, candidate, symlinks=True, copy_function=os.link,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
        rec = {'commit': args.commit, 'source': str(SRC), 'files': {}, 'roots': {}}
        hashes = {}
        for name in FILES:
            path = candidate / name
            old = path.read_bytes()
            new = overlay(name, old.decode()).encode()
            compile(new, name, 'exec')
            d.atomic(path, new)
            hashes[name] = {'before': d.sha(old), 'after': d.sha(new)}
            main = d.MAIN / name
            d.stage_file(folder, rec, main, overlay(name, main.read_text()).encode())
        test = 'tests/test_fb_page_default_english.py'
        d.atomic(candidate / test, (SRC / test).read_bytes())
        result = subprocess.run(['python3', '-m', 'unittest', 'tests.test_fb_page_default_english', '-q'],
                                cwd=candidate, capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        (folder / 'tests.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('candidate tests failed')
        rec['roots']['fb'] = {'before_root': str(origin), 'candidate': str(candidate),
                              'files': hashes, 'state': 'prepared'}
        d.save(folder, rec)
        print(json.dumps(rec))
        return
    rec = json.loads((folder / 'record.json').read_text())
    if rec['commit'] != args.commit:
        raise RuntimeError('commit mismatch')
    reverse = args.action == 'rollback'
    d.check_files(rec, reverse)
    if d.sched(folder, 'snapshot', 'fb', reverse):
        raise RuntimeError('scheduler baseline unhealthy; inspect before switch')
    try:
        d.switch_lane(folder, rec, 'fb', reverse)
        d.install_files(folder, rec, reverse)
    finally:
        restored = d.sched(folder, 'restore', 'fb', reverse)
    if restored:
        raise RuntimeError('scheduler restore needs inspection')
    rec['status'] = 'rolled_back' if reverse else 'active'
    d.save(folder, rec)
    print(json.dumps({'status': rec['status'], 'commit': args.commit, 'root': str(d.ROOTS['fb'].resolve())}))


if __name__ == '__main__':
    main()
