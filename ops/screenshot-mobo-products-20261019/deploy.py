"""Deploy narrow screenshot changes from a verified Git checkout; retain live composite API."""
import argparse
import ast
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
LIVE = Path('/root/drama_material_service')
STATE = Path('/mnt/data-disk/codex/tasks/screenshot-mobo-products-20261019')
FUNCTIONS = ('app_package_for_app_id', 'validate_screenshot_request', 'post_screenshot_ai_source_callback')
URL = '/api/drama-screenshot-material/products'
OLD_URL = '/api/drama-material/products'
WORKERS = ('codex-screenshot-batch', 'codex-screenshot-batch-burst',
           'codex-screenshot-migrated-primary', 'codex-screenshot-migrated-burst')


def command(*args):
    return subprocess.check_output(list(args), universal_newlines=True).strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def function_nodes(source):
    return {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}


def ast_sha(node):
    # ast.dump changed empty-field rendering in Python 3.13, and 3.12 added
    # FunctionDef.type_params. Keep source guards stable across Windows/Linux.
    def canonical(value):
        if isinstance(value, ast.AST):
            return [type(value).__name__, {k: canonical(v) for k, v in ast.iter_fields(value) if k != 'type_params'}]
        if isinstance(value, list):
            return [canonical(v) for v in value]
        if isinstance(value, (bytes, complex)) or value is Ellipsis:
            return [type(value).__name__, repr(value)]
        return value
    return sha(json.dumps(canonical(node), sort_keys=True, separators=(',', ':')).encode())


def compose_app(live_source, repo_source, expected):
    old = function_nodes(live_source)
    new = function_nodes(repo_source)
    lines = live_source.splitlines(keepends=True)
    for name in sorted(FUNCTIONS, key=lambda k: old[k].lineno, reverse=True):
        if ast_sha(old[name]) != expected[name]:
            raise RuntimeError('live screenshot function drift: ' + name)
        lines[old[name].lineno - 1:old[name].end_lineno] = [ast.get_source_segment(repo_source, new[name]) + '\n']
    source = ''.join(lines)
    marker = '        if parsed.path == "' + OLD_URL + '":'
    route_start = repo_source.index('        if parsed.path == "' + URL + '":')
    route_end = repo_source.index(marker, route_start)
    if source.count(marker) != 1 or URL in source:
        raise RuntimeError('product route drift')
    source = source.replace(marker, repo_source[route_start:route_end] + marker)
    # Other API functions must remain AST-identical to the live composite.
    result = function_nodes(source)
    for name, node in old.items():
        if name not in FUNCTIONS and ast_sha(node) != ast_sha(result[name]):
            raise RuntimeError('unrelated API function changed: ' + name)
    return source


def read_db():
    conn = sqlite3.connect('file:' + str(LIVE / 'data/drama_material_jobs.sqlite3') + '?mode=ro', uri=True, timeout=10)
    conn.execute('PRAGMA query_only=ON')
    return conn


def assert_drained():
    with closing(read_db()) as conn:
        for table in ('drama_screenshot_job', 'drama_material_job'):
            count = conn.execute("SELECT COUNT(*) FROM " + table + " WHERE status NOT IN ('done','failed')").fetchone()[0]
            if count:
                raise RuntimeError('active jobs must drain before API restart: ' + table)


def worker_pids():
    return {unit: command('systemctl', 'show', unit, '-p', 'MainPID', '--value') for unit in WORKERS}


def api_token():
    pid = command('systemctl', 'show', 'drama-material-api', '-p', 'MainPID', '--value')
    env = dict(s.split('=', 1) for s in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in s)
    if not env.get('AI_SOURCE_CALLBACK_TOKEN') or env.get('AI_SOURCE_CALLBACK_ENABLED', '1').lower() in ('0', 'false', 'no', 'off'):
        raise RuntimeError('material callback must be configured and enabled')
    token = env.get('AUTO_COVER_API_TOKEN') or env.get('DRAMA_SCREENSHOT_API_TOKEN') or env.get('AI_COVER_API_TOKEN')
    if not token:
        raise RuntimeError('screenshot API token unavailable')
    return token


def verify_api():
    token = api_token()
    req = urllib.request.Request('http://127.0.0.1:8787' + URL, headers={'Authorization': 'Bearer ' + token})
    for attempt in range(15):
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.load(response)
            assert [p['app_id'] for p in data['items']] == ['1479', '979', '3360', '2373']
            return data
        except Exception:
            if attempt == 14:
                raise
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--activate', action='store_true', help='Enable the three-product cron wrapper after real acceptance.')
    args = parser.parse_args()
    assert command('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 5 * 1024 ** 3
    expected = json.loads((ROOT / 'ops/screenshot-mobo-products-20261019/baseline.json').read_text())
    if args.rollback:
        with open(str(LIVE / 'logs/auto_cover_synthesis.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rollback()
        return
    if args.activate:
        activate()
        return
    assert not command('git', '-C', str(ROOT), 'status', '--porcelain')
    commit = command('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
    api_token()
    current = (LIVE / 'app.py').read_text()
    candidate = compose_app(current, (ROOT / 'app.py').read_text(), expected['functions'])
    files = {str(LIVE / 'app.py'): candidate.encode()}
    for rel in ('scripts/auto_submit_cover_synthesis.py', 'scripts/run_auto_cover_synthesis.sh'):
        assert sha((LIVE / rel).read_bytes()) == expected['files'][rel], 'live runner drift: ' + rel
        files[str(LIVE / rel)] = (ROOT / rel).read_bytes()
    # Keep the old one-product entry point until the two real canaries are accepted.
    files[str(LIVE / 'scripts/run_auto_cover_synthesis.sh')] = (LIVE / 'scripts/run_auto_cover_synthesis.sh').read_bytes()
    for path in (LIVE / 'static/screenshots.html', Path('/usr/share/nginx/html/screenshots.html')):
        content = path.read_text()
        assert content.count(OLD_URL) == 1 and URL not in content, 'live page drift'
        files[str(path)] = content.replace(OLD_URL, URL).encode()
    module_path = LIVE / 'features/screenshot_products.py'
    assert not module_path.exists(), 'module already deployed'
    files[str(module_path)] = (ROOT / 'features/screenshot_products.py').read_bytes()
    compile(candidate, '<composite-api>', 'exec')
    print(json.dumps({'mode': 'apply' if args.apply else 'preflight', 'commit': commit,
                      'files': {p: sha(data) for p, data in files.items()}}, indent=2))
    if not args.apply:
        return
    lock = open(str(LIVE / 'logs/auto_cover_synthesis.lock'), 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert_drained()
    pids = worker_pids()
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(str(STATE), 0o700)
    backup = STATE / 'before-deploy'
    backup.mkdir()
    records = []
    for index, path in enumerate(files):
        target = Path(path)
        saved = backup / ('file-%02d' % index)
        record = {'path': path, 'backup': str(saved), 'existed': target.exists(),
                  'expected_sha256': sha(files[path])}
        if target.exists():
            shutil.copy2(str(target), str(saved))
            record['before_sha256'] = sha(saved.read_bytes())
        records.append(record)
    with closing(read_db()) as src:
        with sqlite3.connect(str(backup / 'jobs.sqlite3')) as dst:
            src.backup(dst)
    (backup / 'crontab.txt').write_text(command('crontab', '-l') + '\n')
    state = {'commit': commit, 'files': records, 'worker_pids': pids, 'status': 'prepared'}
    (STATE / 'deployment.json').write_text(json.dumps(state, indent=2))
    assert_drained()
    # The cron is unchanged and remains under the shared lock throughout installation.
    subprocess.check_call(['systemctl', 'stop', 'drama-material-api'])
    try:
        for path, data in files.items():
            target = Path(path)
            temporary = target.with_name(target.name + '.mobo-new')
            temporary.write_bytes(data)
            if target.exists():
                os.chmod(str(temporary), target.stat().st_mode & 0o777)
            else:
                os.chmod(str(temporary), 0o644)
            os.replace(str(temporary), str(target))
        py_compile.compile(str(LIVE / 'app.py'), doraise=True)
        subprocess.check_call(['systemctl', 'start', 'drama-material-api'])
        state['products'] = verify_api()
        assert pids == worker_pids(), 'image worker PIDs changed'
        for record in records:
            record['after_sha256'] = sha(Path(record['path']).read_bytes())
        state['status'] = 'deployed'
        (STATE / 'deployment.json').write_text(json.dumps(state, indent=2))
        print('DEPLOYED', commit)
    except Exception:
        rollback()
        raise


def rollback():
    state = json.loads((STATE / 'deployment.json').read_text())
    assert_drained()
    for record in state['files']:
        target = Path(record['path'])
        if target.exists():
            allowed = {record.get('before_sha256'), record.get('after_sha256'), record.get('expected_sha256')}
            assert sha(target.read_bytes()) in allowed, 'rollback refused because file changed after deployment: ' + str(target)
    subprocess.check_call(['systemctl', 'stop', 'drama-material-api'])
    for record in state['files']:
        target = Path(record['path'])
        if record['existed']:
            shutil.copy2(record['backup'], str(target))
        elif target.exists():
            target.unlink()
    subprocess.check_call(['systemctl', 'start', 'drama-material-api'])
    state['status'] = 'rolled_back'
    (STATE / 'deployment.json').write_text(json.dumps(state, indent=2))
    print('ROLLED_BACK: task database and generated assets retained')


def activate():
    state = json.loads((STATE / 'deployment.json').read_text())
    assert state['status'] == 'deployed'
    assert state['commit'] == command('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
    acceptance = json.loads((STATE / 'acceptance.json').read_text())
    assert acceptance['accepted'] is True
    assert sorted(j['app_id'] for j in acceptance['jobs']) == ['2373', '3360']
    assert len(acceptance['images']) == 6 and len(acceptance['materials']) == 6
    lock = open(str(LIVE / 'logs/auto_cover_synthesis.lock'), 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    target = LIVE / 'scripts/run_auto_cover_synthesis.sh'
    record = next(r for r in state['files'] if r['path'] == str(target))
    assert sha(target.read_bytes()) == record['after_sha256'], 'wrapper drift'
    temporary = target.with_name(target.name + '.mobo-new')
    shutil.copy2(str(ROOT / 'scripts/run_auto_cover_synthesis.sh'), str(temporary))
    os.chmod(str(temporary), target.stat().st_mode & 0o777)
    os.replace(str(temporary), str(target))
    record['after_sha256'] = sha(target.read_bytes())
    state['cron_products_activated'] = True
    (STATE / 'deployment.json').write_text(json.dumps(state, indent=2))
    print('ACTIVATED: existing 10:00 and 22:00 cron now runs all three profiles')


if __name__ == '__main__':
    main()
