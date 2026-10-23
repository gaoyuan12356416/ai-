"""Scoped static release. No API, scheduler, database, COS or service mutation."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

BASE = Path('/mnt/data-disk/report-navigation-20261023')
ROOTS = [Path('/root/drama_material_service/static'), Path('/usr/share/nginx/html')]
EXPECTED = {
    'quick-nav.js': '7b298ebc1ef4fa2591122a0f89d9154a24a2562b4f5e83e5204f4a07495e91b6',
    'social-analytics.html': '905b3b92a6e8085ea5a7653ce3cac142499d587325919692e723a5ebf2ad1951',
    'social-analytics.js': '742ec7e204c5e3f793a0e2221d9cbbc7c97eca3278dec03afdb392d4b8883262',
    'ai-image-daily.html': None,
    'ai-image-daily.js': None,
}
NAV_HASHES = ['b3c145852c4ec737628d159e9adbe5c2d56a24e389e5a4df864ebcc5fa62e197',
              '7db7b71e12c9360d123f0283b11a0304561de1a64871e922e7459c150c07bb8f']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def current(path):
    return sha(path.read_bytes()) if path.exists() else None


def write(path, data, mode=0o644):
    tmp = path.with_name(path.name + '.report-nav.tmp')
    with tmp.open('wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    tmp.chmod(mode)
    os.replace(tmp, path)


def update_navigation(data):
    groups = json.loads(data)
    group = next((g for g in groups if g.get('key') == 'social_reports'), None)
    if group is None:
        group = dict(key='social_reports', order=50, module='social_analytics', items=[dict(
            key='socialAnalytics', label='社媒效果分析', kind='page', href='/social-analytics.html',
            description='按人员、渠道、账号和剧目查看发布与效果', enabled=True, order=10,
            module='social_analytics')])
        groups.append(group)
    group['label'] = '数据报表'
    for item in group['items']:
        if item.get('key') == 'socialAnalytics':
            item['label'] = '社媒效果分析'
    if not any(i.get('key') == 'aiImageDaily' for i in group['items']):
        group['items'].append(dict(key='aiImageDaily', label='AI 图片素材日报',
            description='查看三产品 AI 图片产出、投放效果及历史日报', kind='page',
            href='/ai-image-daily.html', module='social_analytics', enabled=True, order=20))
    return (json.dumps(groups, ensure_ascii=False, indent=2) + '\n').encode()


def rollback(backup):
    manifest = json.loads((backup / 'manifest.json').read_text())
    for row in manifest['files']:
        assert current(Path(row['path'])) in (row['before'], row['after']), 'Deployment drift: ' + row['path']
        if row['before']:
            assert current(backup / row['backup']) == row['before'], 'Backup drift'
    for row in reversed(manifest['files']):
        path = Path(row['path'])
        if row['before']:
            write(path, (backup / row['backup']).read_bytes(), row['mode'])
        elif path.exists():
            path.unlink()
    print(json.dumps(dict(rolled_back=str(backup), services_restarted=False)))


def install(repo, commit):
    uuid = subprocess.check_output(['findmnt', '-no', 'UUID', '/mnt/data-disk'], text=True).strip()
    assert uuid == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 1024**3
    resolved = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', commit + '^{commit}'], text=True).strip()
    assert resolved == commit and len(commit) == 40
    plan = []
    for index, root in enumerate(ROOTS):
        for name, expected in EXPECTED.items():
            path = root / name
            assert current(path) == expected, 'Source drift: ' + str(path)
            data = subprocess.check_output(['git', '-C', str(repo), 'show', commit + ':static/' + name])
            plan.append((path, data))
        nav = root / 'navigation.json'
        assert current(nav) == NAV_HASHES[index], 'Navigation drift: ' + str(nav)
        plan.append((nav, update_navigation(nav.read_bytes())))
    backup = BASE / 'backups' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True)
    manifest = dict(commit=commit, files=[])
    for index, (path, data) in enumerate(plan):
        before = current(path)
        mode = (path.stat().st_mode & 0o777) if before else 0o644
        old = str(index) + '.before'
        if before:
            (backup / old).write_bytes(path.read_bytes())
        manifest['files'].append(dict(path=str(path), before=before, after=sha(data), backup=old, mode=mode))
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    try:
        for (path, data), row in zip(plan, manifest['files']):
            assert current(path) == row['before'], 'Concurrent change: ' + str(path)
            write(path, data, row['mode'])
        assert all(current(Path(row['path'])) == row['after'] for row in manifest['files'])
    except Exception:
        rollback(backup)
        raise
    print(json.dumps(dict(commit=commit, backup=str(backup), files=len(plan), services_restarted=False)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--commit')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if args.rollback:
        rollback(args.rollback)
    else:
        assert args.repo and args.commit
        install(args.repo, args.commit)
