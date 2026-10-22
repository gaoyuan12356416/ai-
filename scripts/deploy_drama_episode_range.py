"""Deploy a SHA-guarded range-identity patch, preserving the live composite."""
import sqlite3
import sys
from pathlib import Path
import deploy_youtube_channel_templates as common
from patch_drama_episode_range import patch_app, patch_page

APP_SHA = '627e47665eb40a1e177bbacd16d3047546608099d28e4b5b916453dcf3c6e68a'
PAGE_SHA = 'c1cb017e1d0995af629691d2c60cdc6c96eadfb1e76e1b61fb235885d51dae17'
PROTECTED = ('drama-material-job-worker.service', 'drama-youtube-unified-writer.service',
             'youtube-auto-publish-worker.service')


def prepare(stage):
    plans = {}
    def add(path, data, expected):
        if common.sha(path) != expected:
            raise RuntimeError('Live file drift: ' + str(path))
        plans[path] = dict(data=data, old=expected, new=common.digest(data),
                          mode=(path.stat().st_mode & 0o777) if path.exists() else 0o644)
    app = common.ROOT / 'app.py'
    if common.sha(app) != APP_SHA:
        raise RuntimeError('Live app changed; rebase the narrow patch')
    add(app, patch_app(app.read_text(encoding='utf-8')).encode(), APP_SHA)
    for page in (common.ROOT / 'static/index.html', common.PUBLIC / 'index.html'):
        if common.sha(page) != PAGE_SHA:
            raise RuntimeError('Live page changed; rebase the narrow patch')
        add(page, patch_page(page.read_text(encoding='utf-8')).encode(), PAGE_SHA)
    rel = 'features/drama_job_identity.py'
    data = (stage / rel).read_bytes()
    compile(data, rel, 'exec')
    add(common.ROOT / rel, data, None)
    return plans


def require_idle():
    conn = sqlite3.connect('file:' + str(common.ROOT / 'data/drama_material_jobs.sqlite3') + '?mode=ro', uri=True)
    try:
        for table in ('drama_material_job', 'drama_screenshot_job'):
            count = conn.execute("SELECT count(*) FROM " + table + " WHERE status NOT IN ('done','failed')").fetchone()[0]
            if count:
                raise RuntimeError('Wait for active jobs before API restart: ' + table)
    finally:
        conn.close()


def main():
    import fcntl
    common.BASE = Path('/mnt/data-disk/deploy/drama-episode-range')
    if common.run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data disk mount mismatch')
    common.BASE.mkdir(parents=True, exist_ok=True)
    with (common.BASE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        protected = {u: common.run('systemctl', 'show', u, '-p', 'MainPID', '-p', 'ActiveState') for u in PROTECTED}
        if '--check' not in sys.argv:
            require_idle()
        common.prepare = prepare
        common.main()
        for unit, state in protected.items():
            if common.run('systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ActiveState') != state:
                raise RuntimeError('Protected service changed: ' + unit)


if __name__ == '__main__':
    main()
