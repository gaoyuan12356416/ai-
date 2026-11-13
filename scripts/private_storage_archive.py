#!/usr/bin/env python3
"""Private, content-addressed COS archives with verified restore and deletion receipts.

Credentials are read from a server-only mode-0600 config; they are never logged.
Large compressible files are gzip streamed into multipart uploads without a local copy.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import stat
import sys
import time
import zlib
from pathlib import Path
from urllib.parse import quote

BLOCK = 4 * 1024 * 1024
PART = 16 * 1024 * 1024
CONFIG = '/etc/codex-private-storage-archive.json'
COMPRESS = {'.sqlite', '.sqlite3', '.db', '.json', '.jsonl', '.csv', '.tsv', '.txt', '.log', '.pkl', '.html'}


def signature(path):
    s = os.lstat(str(path))
    if not stat.S_ISREG(s.st_mode):
        raise ValueError('source must be a regular file, not a link')
    return {'device': s.st_dev, 'inode': s.st_ino, 'size': s.st_size,
            'mtime_ns': s.st_mtime_ns, 'ctime_ns': s.st_ctime_ns,
            'allocated': s.st_blocks * 512 if hasattr(s, 'st_blocks') else s.st_size}


def digest_file(path):
    h = hashlib.sha256()
    with open(str(path), 'rb') as f:
        for b in iter(lambda: f.read(BLOCK), b''):
            h.update(b)
    return h.hexdigest()


def open_inode(sig):
    """Do not remove a file held by a live process, including read-only consumers."""
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    s = fd.stat()
                    if (s.st_dev, s.st_ino) == (sig['device'], sig['inode']):
                        return True
                except OSError:
                    pass
        except OSError:
            pass
    return False


def safe_key(key, prefix):
    if not key.startswith(prefix.rstrip('/') + '/') or '..' in key.split('/'):
        raise ValueError('object key outside configured archive prefix')
    return key


class Archive:
    def __init__(self, config=CONFIG):
        from qcloud_cos import CosConfig, CosS3Client
        self.settings = json.loads(Path(config).read_text())
        c = self.settings['cos']
        self.bucket, self.region = c['bucket'], c['region']
        self.prefix = c.get('prefix', 'storage-archive/v1').strip('/')
        self.client = CosS3Client(CosConfig(Region=c['region'], SecretId=c['secret_id'],
                                          SecretKey=c['secret_key'], Scheme='https', Timeout=120))
        self.checked = {}
        self._private_acl(self.client.get_bucket_acl(Bucket=self.bucket))

    @staticmethod
    def _private_acl(acl):
        for g in acl.get('AccessControlList', {}).get('Grant', []):
            if g.get('Grantee', {}).get('URI'):
                raise RuntimeError('public/group ACL is forbidden for private archives')

    def head(self, key):
        from qcloud_cos.cos_exception import CosServiceError
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except CosServiceError as exc:
            if exc.get_status_code() == 404:
                return None
            raise

    def privacy(self, key):
        import requests
        self._private_acl(self.client.get_object_acl(Bucket=self.bucket, Key=key))
        url = 'https://{}.cos.{}.myqcloud.com/{}'.format(self.bucket, self.region, quote(key, safe='/'))
        with requests.get(url, headers={'Range': 'bytes=0-0'}, stream=True,
                          timeout=30, allow_redirects=False) as response:
            if response.status_code not in (401, 403):
                raise RuntimeError('anonymous access was not denied: HTTP {}'.format(response.status_code))

    def verify(self, key, sha256, size, encoding, output=None):
        safe_key(key, self.prefix)
        result = self.client.get_object(Bucket=self.bucket, Key=key)
        raw = result['Body'].get_raw_stream()
        body = gzip.GzipFile(fileobj=raw, mode='rb') if encoding == 'gzip' else raw
        h, n = hashlib.sha256(), 0
        try:
            while True:
                b = body.read(BLOCK)
                if not b:
                    break
                h.update(b)
                n += len(b)
                if output is not None:
                    output.write(b)
        finally:
            body.close()
            raw.close()
        if n != size or h.hexdigest() != sha256:
            raise RuntimeError('archive full-read checksum/length mismatch')
        self.privacy(key)
        return result

    def upload(self, path, key, sha256, sig, encoding):
        metadata = {'x-cos-meta-source-sha256': sha256,
                    'x-cos-meta-source-size': str(sig['size']),
                    'x-cos-meta-archive-encoding': encoding}
        if sig['size'] == 0:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=b'', ACL='private', Metadata=metadata)
            return
        start = self.client.create_multipart_upload(Bucket=self.bucket, Key=key, ACL='private',
                                                    Metadata=metadata, ContentType='application/octet-stream')
        upload_id, parts, pending = start['UploadId'], [], bytearray()
        compressor = zlib.compressobj(1, zlib.DEFLATED, 31) if encoding == 'gzip' else None
        h = hashlib.sha256()

        def send(data):
            num = len(parts) + 1
            r = self.client.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                        PartNumber=num, Body=data, EnableMD5=True)
            parts.append({'PartNumber': num, 'ETag': r['ETag']})

        try:
            with open(str(path), 'rb') as f:
                while True:
                    b = f.read(BLOCK)
                    if not b:
                        break
                    h.update(b)
                    pending.extend(compressor.compress(b) if compressor else b)
                    while len(pending) >= PART:
                        send(bytes(pending[:PART]))
                        del pending[:PART]
            if compressor:
                pending.extend(compressor.flush())
            if pending:
                send(bytes(pending))
            if h.hexdigest() != sha256 or signature(path) != sig:
                raise RuntimeError('source changed during archive upload')
            self.client.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                                  MultipartUpload={'Part': parts})
        except BaseException:
            try:
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
            except Exception:
                pass
            raise

    def put(self, path, namespace):
        path = Path(path).absolute()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', namespace):
            raise ValueError('invalid archive namespace')
        sig = signature(path)
        sha256 = digest_file(path)
        if signature(path) != sig:
            raise RuntimeError('source changed during hashing')
        encoding = 'gzip' if path.suffix.lower() in COMPRESS and sig['size'] else 'identity'
        key = '{}/objects/{}/{}{}'.format(self.prefix, sha256[:2], sha256, '.gz' if encoding == 'gzip' else '')
        head = self.head(key)
        reused = head is not None
        if head is None:
            self.upload(path, key, sha256, sig, encoding)
            head = self.head(key)
        lower = {k.lower(): str(v) for k, v in head.items()}
        if (lower.get('x-cos-meta-source-sha256') != sha256 or
                lower.get('x-cos-meta-source-size') != str(sig['size']) or
                lower.get('x-cos-meta-archive-encoding') != encoding):
            raise RuntimeError('archive metadata mismatch')
        identity = (key, lower.get('etag'), sig['size'])
        if identity not in self.checked:
            self.verify(key, sha256, sig['size'], encoding)
            self.checked[identity] = True
        if signature(path) != sig:
            raise RuntimeError('source changed before archive receipt')
        receipt = {'bucket': self.bucket, 'region': self.region, 'key': key, 'sha256': sha256,
                   'size': sig['size'], 'stored_size': int(lower['content-length']), 'encoding': encoding,
                   'etag': lower.get('etag', ''), 'verified': True, 'private': True, 'reused': reused,
                   'path': str(path), 'namespace': namespace, 'signature': sig, 'verified_at': time.time()}
        source_stat = os.lstat(str(path))
        receipt.update(source_mode=stat.S_IMODE(source_stat.st_mode),
                       source_uid=source_stat.st_uid, source_gid=source_stat.st_gid,
                       source_nlink=source_stat.st_nlink)
        source_id = hashlib.sha256(str(path).encode()).hexdigest()
        receipt_key = '{}/receipts/{}/{}/{}.json'.format(self.prefix, namespace, source_id, sha256)
        self.client.put_object(Bucket=self.bucket, Key=receipt_key, ACL='private',
                               Body=json.dumps(receipt, sort_keys=True).encode(), ContentType='application/json', EnableMD5=True)
        receipt['receipt_key'] = receipt_key
        return receipt

    def get(self, key, output, sha256, size):
        key = safe_key(key, self.prefix)
        head = self.head(key)
        if head is None:
            raise FileNotFoundError('archive object missing')
        headers = {k.lower(): str(v) for k, v in head.items()}
        if headers.get('x-cos-meta-source-sha256') != sha256 or headers.get('x-cos-meta-source-size') != str(size):
            raise RuntimeError('requested identity does not match archive metadata')
        target = Path(output).absolute()
        if target.exists():
            if target.is_file() and not target.is_symlink() and target.stat().st_size == size and digest_file(target) == sha256:
                return {'path': str(target), 'sha256': sha256, 'size': size, 'verified': True, 'already_present': True}
            raise FileExistsError('restore target exists with different content')
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + '.archive-restore-' + str(os.getpid()))
        try:
            with temp.open('xb') as f:
                os.chmod(str(temp), 0o600)
                self.verify(key, sha256, size, headers['x-cos-meta-archive-encoding'], output=f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(temp), str(target))
        finally:
            if temp.exists():
                temp.unlink()
        return {'path': str(target), 'sha256': sha256, 'size': size, 'verified': True}


def append_receipt(file, row):
    with open(str(file), 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, sort_keys=True) + '\n')
        f.flush()
        os.fsync(f.fileno())


def batch(archive, args):
    plan = json.loads(Path(args.plan).read_text())
    roots = [Path(p).resolve() for p in plan['allowed_roots']]
    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    released = 0
    done = 0
    failures = 0
    for item in plan['files']:
        path = Path(item['path']).absolute()
        resolved = path.resolve()
        if not any(resolved != root and root in resolved.parents for root in roots):
            raise ValueError('plan source outside explicit allowed roots')
        if not path.exists():
            continue
        try:
            if signature(path) != item['signature']:
                raise RuntimeError('source changed since approved plan')
            receipt = archive.put(path, plan['namespace'])
            append_receipt(manifest, dict(receipt, event='verified'))
            if args.delete_verified:
                if signature(path) != receipt['signature'] or open_inode(receipt['signature']):
                    raise RuntimeError('source changed or remains open; not removed')
                # The restore receipt exists in private COS and in a durable local journal.
                links = os.lstat(str(path)).st_nlink
                path.unlink()
                freed = receipt['signature']['allocated'] if links == 1 else 0
                released += freed
                append_receipt(manifest, dict(receipt, event='deleted', deleted_at=time.time(), released_bytes=freed))
            done += 1
            print(json.dumps({'done': done, 'path': str(path), 'released_bytes': released,
                              'source_bytes': receipt['size'], 'stored_bytes': receipt['stored_size']}), flush=True)
        except Exception as exc:
            failures += 1
            # Never render SDK request details, headers, config, or signed URLs.
            append_receipt(manifest, {'event': 'failed', 'path': str(path), 'error_type': type(exc).__name__, 'at': time.time()})
            print(json.dumps({'path': str(path), 'error_type': type(exc).__name__, 'preserved': True}), flush=True)
    print(json.dumps({'complete': True, 'files': done, 'failures': failures, 'released_bytes': released}), flush=True)
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('PRIVATE_STORAGE_ARCHIVE_CONFIG', CONFIG))
    sub = parser.add_subparsers(dest='command', required=True)
    put = sub.add_parser('put')
    put.add_argument('--path', required=True)
    put.add_argument('--namespace', required=True)
    put.add_argument('--json', action='store_true')
    get = sub.add_parser('get')
    get.add_argument('--key', required=True)
    get.add_argument('--output', required=True)
    get.add_argument('--sha256', required=True)
    get.add_argument('--size', required=True, type=int)
    get.add_argument('--json', action='store_true')
    run = sub.add_parser('batch')
    run.add_argument('--plan', required=True)
    run.add_argument('--manifest', required=True)
    run.add_argument('--delete-verified', action='store_true')
    args = parser.parse_args()
    try:
        archive = Archive(args.config)
        if args.command == 'put':
            print(json.dumps(archive.put(args.path, args.namespace)))
        elif args.command == 'get':
            print(json.dumps(archive.get(args.key, args.output, args.sha256, args.size)))
        else:
            return batch(archive, args)
        return 0
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__, 'ok': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
