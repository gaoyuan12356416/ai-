"""Add the new route and merge its item into both live navigation copies."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

source = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(source))
from features.moboreels_library.navigation import ensure_navigation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", required=True)
    deployment = Path(parser.parse_args().deployment).resolve()
    if not str(deployment).startswith("/mnt/data-disk/moboreels-library/deployments/"):
        raise SystemExit("Invalid backup directory")
    deployment.mkdir(parents=True, exist_ok=False, mode=0o700)
    paths = [Path('/root/drama_material_service/static/navigation.json'),
             Path('/usr/share/nginx/html/navigation.json'),
             Path('/etc/nginx/default.d/moboreels-library.conf')]
    record = []
    for index, path in enumerate(paths):
        backup = deployment / ('backup-%d' % index)
        if path.exists(): shutil.copy2(str(path), str(backup))
        record.append({'target': str(path), 'backup': str(backup) if backup.exists() else None})
    (deployment / 'manifest.json').write_text(json.dumps(record, indent=2))
    for path in paths[:2]:
        config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        temporary = path.with_name(path.name + '.moboreels-new')
        temporary.write_text(json.dumps(ensure_navigation(config), ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(path))
    shutil.copy2(str(source / 'deploy/nginx/moboreels-library.conf'), str(paths[2]))
    (deployment / 'rollback.py').write_text('''import json,pathlib,shutil,subprocess
p=pathlib.Path(__file__).resolve().parent
for r in json.loads((p/'manifest.json').read_text()):
 d=pathlib.Path(r['target'])
 if d.name=='navigation.json':
  data=json.loads(d.read_text())
  for group in data: group['items']=[i for i in group.get('items',[]) if i.get('key')!='moboreelsLibrary']
  data=[g for g in data if g.get('key')!='partner_libraries' or g.get('items')]
  d.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
 elif r['backup']: shutil.copy2(r['backup'],str(d))
 elif d.exists(): d.unlink()
subprocess.run(['nginx','-t'],check=True)
subprocess.run(['systemctl','reload','nginx'],check=True)
''', encoding='utf-8')
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
    print(json.dumps({'routes_activated': True, 'backup': str(deployment)}))


if __name__ == '__main__': main()
