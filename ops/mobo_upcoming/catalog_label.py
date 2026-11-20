"""Publish the catalog timer description while preserving other page edits."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


NAME = 'moboreels-library.html'
OLD = '每天北京时间 09:00 自动同步'.encode()
NEW = '北京时间每小时整点自动同步'.encode()
SOURCE = Path(__file__).resolve().parents[2]


def install(path, old, new):
    raw = path.read_bytes()
    if raw.count(new) == 1 and old not in raw:
        return
    if raw.count(old) != 1 or new in raw:
        raise ValueError(f'目录更新说明已变化，请核对 {path}')
    stat = path.stat()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(raw.replace(old, new))
            stream.flush()
            os.fchown(stream.fileno(), stat.st_uid, stat.st_gid)
            os.fchmod(stream.fileno(), stat.st_mode & 0o7777)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('apply','rollback'))
    parser.add_argument('--folder', required=True, type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    uuid = subprocess.check_output(['findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk'], text=True).strip()
    if Path('/mnt/data-disk') not in folder.parents or uuid != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise ValueError('unexpected backup location')
    manifest = folder / 'page-paths.json'
    if args.action == 'apply' and not manifest.exists():
        page = (SOURCE / 'static' / NAME).read_bytes()
        if page.count(NEW) != 1 or OLD in page:
            raise ValueError('source description changed')
        pid = subprocess.check_output(['systemctl','show','moboreels-library','-p','MainPID','--value'], text=True).strip()
        paths = list(dict.fromkeys(p.resolve() for p in (Path('/usr/share/nginx/html') / NAME,
                     Path('/root/drama_material_service/static') / NAME, Path('/proc') / pid / 'cwd/static' / NAME)))
        folder.mkdir(mode=0o750, parents=True, exist_ok=True)
        for index, path in enumerate(paths):
            shutil.copy2(path, folder / f'page-{index}.before')
        manifest.write_text(json.dumps([str(p) for p in paths], indent=2))
    paths = json.loads(manifest.read_text())
    old, new = (OLD, NEW) if args.action == 'apply' else (NEW, OLD)
    for path in paths:
        install(Path(path), old, new)
    print(json.dumps(dict(status=args.action, files=len(paths))))


if __name__ == '__main__':
    main()
