"""Small, versioned table views built from the same immutable report as a live query."""
import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path

from .adapters import PLATFORMS
from .report import table_view


def ranges(today):
    yesterday = today - timedelta(days=1)
    return [(end - timedelta(days=days - 1), end)
            for days, end in ((7, yesterday), (1, today), (1, yesterday), (30, yesterday))]


@lru_cache(maxsize=1)
def engine_id():
    directory = Path(__file__).parent
    files = ['prepared_views', 'report', 'warehouse_reader', 'routes', 'backend_scope', 'tiktok_scope', 'adapters', 'bill_income']
    digest = hashlib.sha256()
    for path in [directory / (name + '.py') for name in files] + [directory.parent / 'youtube_analytics/report.py']:
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def directory_for(root, version, today):
    identifier = hashlib.sha256(version.encode()).hexdigest()[:24]
    return Path(root) / 'prepared-web-views' / (today.isoformat() + '-' + identifier + '-' + engine_id())


def view_path(directory, actor, start, end, groups):
    tenant, user, role = (actor.get(key) for key in ('tenant_key', 'user_id', 'role'))
    if not tenant or not user or role not in ('admin', 'user') or actor.get('_system_report'):
        return None
    # visible() resolves administrators at tenant scope and ordinary users at owner scope.
    scope = [tenant, role, user if role == 'user' else None, start, end, groups]
    return directory / (hashlib.sha256(json.dumps(scope).encode()).hexdigest() + '.json')


def read_options(root, manifest, actor):
    directory = directory_for(root, manifest['version'], datetime.now(timezone.utc).date())
    path = view_path(directory, actor, '', '', ['options'])
    if path is not None:
        try:
            return json.loads(path.read_bytes())
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logging.getLogger(__name__).warning('Unreadable prepared filter options', exc_info=True)
    return None


def read_prepared(root, manifest, actor, params, include_titles):
    if params['search'] or any(params['filters'].values()) or params['groups'] not in (
            ['date', 'platform'], ['date', 'platform', 'drama']):
        return None
    now = datetime.now(timezone.utc)
    directory = directory_for(root, manifest['version'], now.date())
    path = view_path(directory, actor, params['start'], params['end'], params['groups'])
    if path is None:
        return None
    try:
        report = json.loads(path.read_bytes())
        return present(report, manifest, params, include_titles, now)
    except FileNotFoundError:
        return None
    except (OSError, KeyError, TypeError, ValueError):
        logging.getLogger(__name__).warning('Unreadable prepared social report. Using live calculation.', exc_info=True)
        return None


def present(report, manifest, params, include_titles, now):
    age = max(0, (now - datetime.fromisoformat(manifest['generated_at'])).total_seconds())
    if report['meta']['warehouse_version'] != manifest['version'] or report['meta']['snapshot_stale'] != (age > 7200):
        return None
    report['meta']['snapshot_age_seconds'] = round(age)
    report.update(table_view(report['all_rows'], params))
    # English titles are attached to table/CSV rows after ranking in the live path.
    for row in report['ranking']:
        row.pop('drama_english_name', None)
        row.pop('drama_english_status', None)
    if not include_titles and 'drama' in params['groups']:
        for row in report['all_rows']:
            row.pop('drama_english_name', None)
            row.pop('drama_english_status', None)
    return report


def pack_report(report):
    return json.dumps({k: v for k, v in report.items() if k not in ('rows', 'ranking', 'pagination')},
                      ensure_ascii=False, separators=(',', ':')).encode()


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        name = stream.name
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            os.unlink(name)
            raise
    os.chmod(name, 0o640)
    os.replace(name, path)


def prepare(root):
    from .warehouse_reader import build_report, catalogs, current
    snapshot = current(root)
    manifest = snapshot[0]
    today = datetime.now(timezone.utc).date()
    directory = directory_for(root, manifest['version'], today)
    if (directory / 'complete').is_file():
        return
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    catalog, _, _, _ = catalogs(root, list(PLATFORMS), snapshot)
    owners = sorted({(row['tenant'], row['owner']) for row in catalog
                     if row.get('tenant') and row.get('owner')})
    # No privileged HTTP session is created. Files are read only after ordinary route authorization.
    admins = [dict(tenant_key=tenant, role='admin', user_id='prepared-tenant-view') for tenant in sorted({t for t, _ in owners})]
    actors = admins + [dict(tenant_key=t, user_id=u, role='user') for t, u in owners]
    settings = {'SOCIAL_ANALYTICS_WAREHOUSE_ROOT': str(root), 'SOCIAL_ANALYTICS_FROZEN_SNAPSHOT': snapshot,
                'SOCIAL_ANALYTICS_BUILDING_VIEWS': True}
    count = 0
    for start, end in ranges(today):
        groups_list = [['date', 'platform']]
        if start == end:
            groups_list.append(['date', 'platform', 'drama'])
        for groups in groups_list:
            query = {'start': [start.isoformat()], 'end': [end.isoformat()], 'group_by': [','.join(groups)]}
            for actor in actors:
                if current(root)[0]['version'] != manifest['version']:
                    return
                path = view_path(directory, actor, start.isoformat(), end.isoformat(), groups)
                if path.is_file():
                    continue
                result = build_report(settings, actor, query, include_titles=True)
                atomic_write(path, pack_report(result))
                count += 1
    from .routes import build_options
    for actor in actors:
        path = view_path(directory, actor, '', '', ['options'])
        if not path.is_file():
            result = build_options(settings, actor)
            atomic_write(path, json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode())
    atomic_write(directory / 'complete', b'ok\n')
    # Only generated files in our own version directories are eligible for cleanup.
    generations = sorted((p for p in directory.parent.iterdir() if p.is_dir() and not p.is_symlink()
                          and re.fullmatch(r'\d{4}-\d{2}-\d{2}-[0-9a-f]{24}-[0-9a-f]{16}', p.name)),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    for old in generations[2:]:
        if old != directory:
            for file in old.iterdir():
                if file.name == 'complete' or re.fullmatch(r'[0-9a-f]{64}\.json', file.name):
                    file.unlink()
            if not any(old.iterdir()):
                old.rmdir()
    print('prepared %d views for %s' % (count, manifest['version']), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    prepare(parser.parse_args().root)
