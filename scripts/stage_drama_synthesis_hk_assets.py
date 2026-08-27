#!/usr/bin/env python3
"""Operator-only HK bootstrap: scoped secrets and a private COS asset transfer.

Runs locally with Paramiko and an already trusted SSH key. Never prints secrets,
copies a whole environment file, modifies source assets, or opens a public port.
The private transfer object and source archive are retained for audited cleanup.
"""
from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

SOURCE_HOST = "43.166.178.132"
TARGET_HOST = "43.154.250.89"
SOURCE_PYTHON = "/root/miniconda3/envs/drama-voice/bin/python"
TARGET_PYTHON = "/data/drama-synthesis-gpu/runtime/py310-cu124-v1/bin/python"

READ_SECRETS = r'''
import json, pathlib
allowed = {'COS_SECRET_ID','COS_SECRET_KEY','COS_BUCKET','COS_REGION','COS_DOMAIN','COS_PREFIX','GPU_VIDEO_WORKER_TOKEN'}
values = {}
for line in pathlib.Path('/root/drama_material_service/.env').read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    key, value = line.split('=', 1)
    if key.strip() in allowed:
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key.strip()] = value
if set(values) != allowed or any(not values[k] for k in allowed - {'COS_PREFIX'}):
    raise RuntimeError('required rendering configuration unavailable')
if values['COS_BUCKET'] != 'advertising-1306474899' or values['COS_REGION'] != 'ap-hongkong':
    raise RuntimeError('unexpected rendering COS target')
print(json.dumps(values))
'''

WRITE_SECRETS = r'''
import hashlib, json, os, pathlib, sys
allowed = {'COS_SECRET_ID','COS_SECRET_KEY','COS_BUCKET','COS_REGION','COS_DOMAIN','COS_PREFIX','GPU_VIDEO_WORKER_TOKEN'}
values = json.load(sys.stdin)
if set(values) != allowed or any(not isinstance(v, str) or any(c in v for c in '\r\n\0') for v in values.values()):
    raise RuntimeError('invalid scoped rendering configuration')
path = pathlib.Path('/etc/drama-synthesis-gpu/worker.env')
data = ''.join(k + '=' + json.dumps(values[k], ensure_ascii=True) + '\n' for k in sorted(values)).encode()
if path.exists():
    if path.is_symlink() or path.read_bytes() != data:
        raise RuntimeError('existing environment differs; explicit review required')
else:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
st = path.stat()
if st.st_uid != 0 or st.st_mode & 0o777 != 0o600:
    raise RuntimeError('unexpected environment owner or mode')
print(json.dumps({'path':str(path),'key_count':len(values),'sha256':hashlib.sha256(data).hexdigest(),'mode':'0600'}))
'''

UPLOAD_ASSETS = r'''
import hashlib, json, os, pathlib, re, shutil, sys, tarfile
import requests
from qcloud_cos import CosConfig, CosS3Client
request = json.load(sys.stdin)
if set(request) != {'resume_archive_sha256'}:
    raise RuntimeError('unexpected upload request')
resume_sha = request['resume_archive_sha256']
if resume_sha is not None and not re.fullmatch(r'[0-9a-f]{64}', resume_sha):
    raise RuntimeError('invalid reviewed resume archive hash')
root = pathlib.Path('/data/fb-page-random-overlay/assets/v1')
expected = '028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f'
raw = (root / 'manifest.json').read_bytes()
if hashlib.sha256(raw).hexdigest() != expected:
    raise RuntimeError('source manifest changed')
manifest = json.loads(raw)
rows = [r for group in manifest['categories'].values() for r in group]
if len(rows) != 20 or sum(r['size'] for r in rows) != 520297533:
    raise RuntimeError('source asset cardinality changed')
for row in rows:
    name = row['name']
    if pathlib.PurePosixPath(name).name != name:
        raise RuntimeError('unexpected source path')
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size != row['size']:
        raise RuntimeError('invalid source asset')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != row['sha256']:
        raise RuntimeError('source asset hash mismatch')
staging = pathlib.Path('/data/drama-synthesis-transfer/20260827')
staging.mkdir(mode=0o700, parents=True, exist_ok=True)
if shutil.disk_usage(staging).free < 1024**3:
    raise RuntimeError('insufficient source staging capacity')
archive = staging / 'fb-v3-028326ab2114.tar'
if archive.is_symlink():
    raise RuntimeError('source archive is a symlink')
if archive.exists() and resume_sha is None:
    raise RuntimeError('source archive already exists; verify it before any retry')
if not archive.exists():
    if resume_sha is not None:
        raise RuntimeError('reviewed resume archive is missing')
    with tarfile.open(archive, 'x') as bundle:
        for name in ['manifest.json'] + sorted(r['name'] for r in rows):
            bundle.add(root / name, arcname=name, recursive=False)
os.chmod(archive, 0o600)
digest = hashlib.sha256()
with archive.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024*1024), b''):
        digest.update(chunk)
archive_sha = digest.hexdigest()
if resume_sha is not None and archive_sha != resume_sha:
    raise RuntimeError('reviewed resume archive hash changed')
values = {}
for line in pathlib.Path('/root/drama_material_service/.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key, value = line.split('=', 1)
        if key.strip() in {'COS_SECRET_ID','COS_SECRET_KEY','COS_BUCKET','COS_REGION'}:
            values[key.strip()] = value.strip().strip('"').strip("'")
if values.get('COS_BUCKET') != 'advertising-1306474899' or values.get('COS_REGION') != 'ap-hongkong':
    raise RuntimeError('unexpected transfer COS target')
client = CosS3Client(CosConfig(Region=values['COS_REGION'], SecretId=values['COS_SECRET_ID'], SecretKey=values['COS_SECRET_KEY'], Timeout=300))
key = 'drama-synthesis-setup/20260827/' + archive_sha + '/fb-v3.tar'
# The SDK validates existing multipart ETags against these same local bytes
# before resuming. The reviewed hash guard above prevents a different archive.
client.upload_file(Bucket=values['COS_BUCKET'], Key=key, LocalFilePath=str(archive), PartSize=8, MAXThread=2, ACL='private', EnableMD5=True)
head = client.head_object(Bucket=values['COS_BUCKET'], Key=key)
if int(head['Content-Length']) != archive.stat().st_size:
    raise RuntimeError('private transfer object size mismatch')
anonymous_url = 'https://' + values['COS_BUCKET'] + '.cos.' + values['COS_REGION'] + '.myqcloud.com/' + key
if requests.head(anonymous_url, timeout=(10, 30), allow_redirects=False).status_code != 403:
    raise RuntimeError('private transfer anonymous access was not denied')
print(json.dumps({'key':key,'sha256':archive_sha,'size':archive.stat().st_size,'source_archive':str(archive),'manifest_sha256':expected}))
'''

DOWNLOAD_ASSETS = r'''
import hashlib, json, os, pathlib, sys, tarfile
from qcloud_cos import CosConfig, CosS3Client
meta = json.load(sys.stdin)
if set(meta) != {'key','sha256','size','source_archive','manifest_sha256'} or meta['key'] != 'drama-synthesis-setup/20260827/' + meta['sha256'] + '/fb-v3.tar':
    raise RuntimeError('unexpected private transfer identity')
values = {}
for line in pathlib.Path('/etc/drama-synthesis-gpu/worker.env').read_text().splitlines():
    key, value = line.split('=', 1)
    values[key] = json.loads(value)
client = CosS3Client(CosConfig(Region=values['COS_REGION'], SecretId=values['COS_SECRET_ID'], SecretKey=values['COS_SECRET_KEY']))
archive = pathlib.Path('/data/drama-synthesis-gpu/install-cache/fb-v3-' + meta['sha256'] + '.tar')
if archive.exists():
    raise RuntimeError('target transfer archive already exists; verify before retry')
client.download_file(Bucket=values['COS_BUCKET'], Key=meta['key'], DestFilePath=str(archive), PartSize=8, MAXThread=4)
digest = hashlib.sha256()
with archive.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024*1024), b''):
        digest.update(chunk)
if archive.stat().st_size != meta['size'] or digest.hexdigest() != meta['sha256']:
    raise RuntimeError('downloaded transfer archive mismatch')
target = pathlib.Path('/data/drama-synthesis-gpu/assets/fb-v3-028326ab2114')
target.mkdir(mode=0o755)
with tarfile.open(archive) as bundle:
    members = bundle.getmembers()
    if len(members) != 21 or len({m.name for m in members}) != 21:
        raise RuntimeError('invalid archive members')
    for member in members:
        if not member.isfile() or pathlib.PurePosixPath(member.name).name != member.name or member.name in ('.','..'):
            raise RuntimeError('unsafe archive entry')
        with bundle.extractfile(member) as src, (target / member.name).open('xb') as dst:
            for chunk in iter(lambda: src.read(1024*1024), b''):
                dst.write(chunk)
        os.chmod(target / member.name, 0o444)
raw = (target / 'manifest.json').read_bytes()
if hashlib.sha256(raw).hexdigest() != meta['manifest_sha256']:
    raise RuntimeError('downloaded manifest mismatch')
rows = [r for group in json.loads(raw)['categories'].values() for r in group]
for row in rows:
    path = target / row['name']
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != row['sha256'] or path.stat().st_size != row['size']:
        raise RuntimeError('downloaded asset hash mismatch')
meta.update({'asset_root':str(target),'verified_files':len(rows),'asset_bytes':sum(r['size'] for r in rows)})
pathlib.Path('/data/drama-synthesis-gpu/evidence/asset-transfer.json').write_text(json.dumps(meta,indent=2))
print(json.dumps(meta))
'''


def remote_json(client, interpreter: str, code: str, payload=None):
    stdin, stdout, stderr = client.exec_command(
        shlex.quote(interpreter) + " -c " + shlex.quote(code), timeout=1800
    )
    if payload is not None:
        stdin.write(json.dumps(payload))
    stdin.channel.shutdown_write()
    result = stdout.read()
    error = stderr.read()
    status = stdout.channel.recv_exit_status()
    if status:
        # Error text is deliberately not emitted: a library exception may carry
        # an authenticated URL or another credential-derived field.
        raise RuntimeError(f"remote operation failed (exit={status}, stderr_bytes={len(error)})")
    return json.loads(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("secrets", "assets"))
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--known-hosts", required=True, type=Path)
    parser.add_argument("--resume-archive-sha256", help="Resume only an operator-verified existing archive hash")
    parser.add_argument("--apply", action="store_true", help="Explicitly perform the scoped staging operation")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required; this command creates scoped bootstrap artifacts")
    if args.resume_archive_sha256 and args.action != "assets":
        parser.error("--resume-archive-sha256 only applies to assets")
    import paramiko
    clients = []
    try:
        for host in (SOURCE_HOST, TARGET_HOST):
            client = paramiko.SSHClient()
            client.load_host_keys(str(args.known_hosts))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(host, username="root", key_filename=str(args.key), allow_agent=False, look_for_keys=False, timeout=15)
            clients.append(client)
        source, target = clients
        if args.action == "secrets":
            values = remote_json(source, SOURCE_PYTHON, READ_SECRETS)
            result = remote_json(target, TARGET_PYTHON, WRITE_SECRETS, values)
        else:
            result = remote_json(source, SOURCE_PYTHON, UPLOAD_ASSETS,
                                 {"resume_archive_sha256": args.resume_archive_sha256})
            print(json.dumps({"stage":"private_cos_uploaded", "size":result["size"], "sha256":result["sha256"]}), flush=True)
            result = remote_json(target, TARGET_PYTHON, DOWNLOAD_ASSETS, result)
        print(json.dumps(result, sort_keys=True))
    finally:
        for client in clients:
            client.close()


if __name__ == "__main__":
    main()
