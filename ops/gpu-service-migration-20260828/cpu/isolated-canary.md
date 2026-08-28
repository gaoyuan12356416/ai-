# CPU isolated image samples — prepared, not executed

Run only after explicit coordinator authorization, US image services/tunnels
have stopped, CPU storage has switched, and the three new CPU workers are healthy.
Keep material admission fenced until verification finishes. These requests go
directly to the new local sidecars; they never use the main API, production job
database, publishing queues or completion callbacks.

Use one retained source image. Generate square and landscape on new primary
`18795`, portrait on new burst `18798`, and cover on new `18790`. Repeat screenshot
requests with different output paths to test cache hits without more generation.
This checks the actual installed workers, not substitute services. Public sample
paths are isolated under `_migration_canary/<run-id>` and physically reside on
the CPU data disk. Retain samples for review; do not insert them into a media pool.

Expected screenshot sizes come from the current CPU `SCREENSHOT_SPECS`:
square **1200×1200**, landscape **1200×628**, portrait **1200×1500**.
Check the cover's actual ratio against 16:9; do not silently accept or resize a
failed ratio as successful AI cover generation.

## 1. Prepare payloads after authorization

The verified input exists at the path below. If it no longer exists, choose a
retained input explicitly; do not download an unrelated substitute. This block
creates files but does not submit generation requests.

```bash
python3 -B - <<'PY'
import hashlib, json, os, pathlib, shutil, sqlite3, subprocess
from PIL import Image

run_id = 'gpu-service-migration-20260828T1502'
base = pathlib.Path('/mnt/data-disk/codex-workers/us-migrated')
out = base / 'canary' / run_id
source = pathlib.Path('/mnt/data-disk/codex-workers/cover/jobs/72ccebff158845ee9e28bb171d285c72/source.jpg')
public = pathlib.Path('/usr/share/nginx/html/drama-screenshot-materials')
cover_public = pathlib.Path('/usr/share/nginx/html/drama-materials')
assert source.is_file() and not out.exists()
with Image.open(str(source)) as input_image:
    input_image.verify()
for path in (public, cover_public):
    assert path.is_symlink(), 'storage has not switched'
    uuid = subprocess.check_output(['findmnt', '-nro', 'UUID', '-T', str(path)], text=True).strip()
    assert uuid == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
for unit in ('codex-screenshot-migrated-primary.service', 'codex-screenshot-migrated-burst.service', 'codex-cover-migrated.service'):
    assert subprocess.check_output(['systemctl', 'is-active', unit], text=True).strip() == 'active'
os.umask(0o077)
out.mkdir(parents=True, mode=0o700)
shutil.copy2(str(source), str(out / 'source.jpg'))
def write(name, value):
    path = out / name
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
with sqlite3.connect('file:/root/drama_material_service/data/drama_material_jobs.sqlite3?mode=ro', uri=True) as conn:
    conn.execute('PRAGMA query_only=ON')
    before = {table: dict(conn.execute('SELECT status,COUNT(*) FROM '+table+' GROUP BY status'))
              for table in ('drama_material_job', 'drama_screenshot_job', 'ad_material_task', 'ad_material_asset')}
write('business-counts-before.json', before)
specs = [('square_1x1', '1:1', 1200, 1200),
         ('landscape_1_91x1', '1.91:1', 1200, 628),
         ('portrait_4x5', '4:5', 1200, 1500)]
# The source path is local. This stable, isolated URL is only the cache identity;
# valid local input prevents the download fallback from being used.
source_url = 'http://127.0.0.1:18795/files/_migration_canary/'+run_id+'/source.jpg'
for lane, selected in (('primary', specs[:2]), ('burst', specs[2:])):
    for phase in ('first', 'cache'):
        prefix = lane+'-'+phase
        payload = {'job_id': 'migration-'+run_id+'-'+prefix,
                   'source_path': str(out/'source.jpg'), 'source_url': source_url,
                   'drama_name': '', 'items': []}
        for key, ratio, width, height in selected:
            payload['items'].append({'key': key, 'ratio': ratio, 'width': width, 'height': height,
                'workspace_output_path': str(out/'outputs'/prefix/(key+'.jpg')),
                'public_output_path': str(public/'_migration_canary'/run_id/prefix/(key+'.jpg'))})
        write('request-'+prefix+'.json', payload)
write('request-cover.json', {'job_id': 'migration-'+run_id+'-cover',
      'source_path': str(out/'source.jpg'), 'source_url': '', 'drama_name': '',
      'workspace_output_path': str(out/'outputs/cover/cover_16x9.jpg'),
      'public_output_path': str(cover_public/'_migration_canary'/run_id/'cover_16x9.jpg')})
write('input.json', {'source_path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
print(str(out))
PY
```

## 2. Run authorized generation, then cache checks

The following block makes real image-generation requests. **Do not run it during
preparation.** The coordinator must explicitly set the acknowledgement variable
for this sample phase. Run the client in a monitored execution session that
yields output, rather than blocking the operator interface for the full timeout.
On an HTTP/client timeout, stop dispatching; inspect the worker and its children
before retrying. Never assume a client timeout cancelled the server request.

```bash
# Set only after the coordinator explicitly authorizes isolated samples:
# export CPU_CANARY_GENERATION_AUTHORIZED=yes
python3 -B - <<'PY'
import hashlib, json, os, pathlib, requests, time
assert os.environ.get('CPU_CANARY_GENERATION_AUTHORIZED') == 'yes'
out = pathlib.Path('/mnt/data-disk/codex-workers/us-migrated/canary/gpu-service-migration-20260828T1502')
checks = [('primary-first',18795), ('burst-first',18798), ('cover',18790),
          ('primary-cache',18795), ('burst-cache',18798)]
for name, port in checks:
    target = out/('response-'+name+'.json')
    intent = out/('dispatch-'+name+'.json')
    assert not target.exists() and not intent.exists(), 'inspect any prior dispatch before repeating'
    route = '/api/codex-cover/generate' if name == 'cover' else '/api/codex-screenshot/generate'
    body = (out/('request-'+name+'.json')).read_bytes()
    with intent.open('x', encoding='utf-8') as handle:
        json.dump({'port':port, 'route':route, 'epoch':time.time(),
                   'request_sha256':hashlib.sha256(body).hexdigest()}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    print('dispatch', name, flush=True)
    # No retries; an ambiguous timeout requires inspection, not another POST.
    response = requests.post('http://127.0.0.1:%d%s' % (port, route),
        data=body,
        headers={'Content-Type':'application/json'}, timeout=(10,7200))
    with target.open('x', encoding='utf-8') as handle:
        handle.write(response.text)
    response.raise_for_status()
    result = response.json()
    assert result.get('status') == 'done', name
    if name.endswith('-cache'):
        assert all(item.get('cache') == 'hit' for item in result['items']), name
    print('complete', name, response.status_code, flush=True)
PY
```

## 3. Verify files, endpoints and unchanged business counts

For each response, verify every requested key exists, screenshot dimensions are
exact, file bytes match workspace/public/returned `/files/` URL, and the equivalent
`https://ai.yingliangads.com/drama-screenshot-materials/...` or
`/drama-materials/...` URL returns the same SHA256. Returned URLs must have no
`..` segment and point to the expected local sidecar. Use Pillow to inspect cover
dimensions and require `abs((width/height)/(16/9)-1) <= 0.08`; also review the
actual cover image and preservation of source characters/title.

Compare the four business status/count dictionaries against
`business-counts-before.json`; no production job is created or retried by this
test. Confirm original CPU worker PIDs remain unchanged, all new service ports
still belong to the intended local worker PIDs, and all sample, job, CLI, cache
and log paths resolve to `/mnt/data-disk`. Preserve request/response files and
samples for the migration report.

The repeated cache requests validate cache reuse and distinct output paths. They
do **not** prove a real failed-dimension retry. Never change a production job or
delete a production cache to manufacture a failure.

## 4. Offline failed-dimension retry contract

The retry controller is the **main app**, not the frozen screenshot sidecar.
`tools/test_single_process_screenshot_batch.py` exercises real Codex generation
and reports model retry metadata; it does not cover the main-app retry contract.
No existing Python test referencing `retry_screenshot_job` or
`process_screenshot_job` was found in the checked-out repository.

`cpu/tests/test_screenshot_retry_contract.py` now executes the actual selected
function definitions from `app.py` via AST extraction, without importing the
main app or evaluating its top-level configuration. All DB, queue, callback,
download and image-generation dependencies are mocked; files are created only
inside a private temporary directory. The case exercises:

- A batch writes two successful dimensions; the third dimension exhausts its
  automatic per-item retry budget and the job is recorded as failed by the
  mocked outer runner.
- The real `retry_screenshot_job` preserves both successes and queues one retry.
- The real `process_screenshot_job` submits only the missing dimension. Existing
  success URLs and both workspace/public file SHA256 and mtime remain unchanged.

This proves the application selection/preservation contract with injected
offline failures. It does not test production queue scheduling, live HTTP
failure recovery, real image quality, or an actual Codex failure. A `done` job's
explicit remake intentionally clears all dimensions and is a different path.

On 2026-08-28, all 15 CPU package tests passed locally. All ten extracted
functions' source text (UTF-8 with normalized LF newlines) matched the current
CPU `/root/drama_material_service/app.py` exactly. The four core functions are
listed below. The app files as a whole are different, so do not deploy the
checkout's app over the live one.

| Function | Current CPU lines | Source SHA256 |
| --- | --- | --- |
| `retry_screenshot_job` | 33586–33641 | `191bce58ab56856b69a568bfa143ffdc8d9c8ce4fed2ceb680e081345c5192c7` |
| `process_screenshot_job` | 33130–33467 | `dfc7935b1b39a375f3ba954822b09a24e65bea98d9ffd29e7b4b91adc2de2161` |
| `cleanup_screenshot_output_paths` | 33119–33127 | `045c5e0b3280fd5f60ce2241b04c93d4a8ab8ec28e69fcdf98f61d321ecd82e6` |
| `file_ready` | 9627–9659 | `391aacd533492f1c67fa8251cf9180f0da35b0d40d51382a886df28c19cee80c` |

Local command from the repository root:

```bash
python -B -m unittest discover -s ops/gpu-service-migration-20260828/cpu/tests -v
```

After the coordinator commits/pushes this test and checks out that exact commit
on CPU, the same test may target the live app read-only. Run from the authorized
Git checkout; temporary fixture files must stay on the data disk:

```bash
install -d -m 700 /mnt/data-disk/migrations/gpu-service-migration-20260828T1502/cpu/test-tmp
TMPDIR=/mnt/data-disk/migrations/gpu-service-migration-20260828T1502/cpu/test-tmp \
SCREENSHOT_RETRY_APP_PATH=/root/drama_material_service/app.py \
python3 -B -m unittest discover -s ops/gpu-service-migration-20260828/cpu/tests -v
```

The CPU invocation above was prepared, not executed before Git deployment.
