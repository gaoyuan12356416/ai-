"""Owner-scoped multipart uploads to Hong Kong COS; the CPU receives metadata only."""
import base64
from contextlib import contextmanager
import hashlib
import json
import math
import re
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .runtime import Fault, MAX_BYTES, check, ident, owner, packed, public_asset

PART_BYTES = 16 * 1024**2
PREFIX = 'ai-video-editing/v1/uploads/'
LEASE_SECONDS = 600


def status_code(exc):
    get = getattr(exc, 'get_status_code', lambda: 0)
    try:
        return int(get())
    except (ValueError, TypeError):
        return 0


class CloudUploads:
    def __init__(self, runtime, client, bucket, region):
        self.rt, self.client, self.bucket, self.region = runtime, client, bucket, region
        self._locks = [threading.RLock() for _ in range(64)]
        with runtime.db(True) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS cloud_uploads(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, asset_id TEXT NOT NULL, remote_id TEXT NOT NULL DEFAULT '',
                parts TEXT NOT NULL DEFAULT '{}', etag TEXT NOT NULL DEFAULT '',
                lease TEXT NOT NULL DEFAULT '', expires REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(owner, operation));
                CREATE INDEX IF NOT EXISTS cloud_uploads_queue ON cloud_uploads(status,expires,created);''')

    @contextmanager
    def lock(self, key):
        with self._locks[int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % len(self._locks)]:
            yield

    def configured(self):
        check(self.client and self.bucket and self.region == 'ap-hongkong',
              'upload_not_configured', '香港上传暂不可用，请稍后重试', 503)

    def key(self, row):
        return PREFIX + row['id'] + '/source.mp4'

    def row(self, uid, actor=None, db=None):
        if db is None:
            with self.rt.db() as connection:
                return self.row(uid, actor, connection)
        result = db.execute('SELECT * FROM cloud_uploads WHERE id=?', (uid,)).fetchone()
        check(result and (actor is None or result['owner'] == owner(actor)),
              'upload_missing', '上传记录不存在', 404)
        return dict(result)

    def update(self, uid, **values):
        values['updated'] = time.time()
        with self.rt.db(True) as db:
            db.execute('UPDATE cloud_uploads SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                       (*values.values(), uid))
        return self.row(uid)

    def dto(self, row, uploaded=None):
        payload = json.loads(row['payload'])
        result = {'id': row['id'], 'status': row['status'], 'part_size': PART_BYTES,
                  'part_count': math.ceil(payload['size'] / PART_BYTES), 'size': payload['size'],
                  'name': payload['name'], 'error': row['error'], 'uploaded_parts': uploaded or []}
        if row['status'] == 'ready':
            tenant, user = json.loads(row['owner'])
            result['asset'] = public_asset(self.rt.asset(row['asset_id'], {'tenant_key': tenant, 'user_id': user}))
        return result

    def create(self, actor, body):
        self.configured()
        operation = body.get('operation_id', '')
        check(isinstance(operation, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}', operation),
              'operation_invalid', '上传操作标识无效')
        payload = {k: body.get(k, default) for k, default in
                   [('name', ''), ('series', ''), ('episode', 0), ('size', 0)]}
        check(type(payload['size']) is int and 0 < payload['size'] <= MAX_BYTES,
              'size_invalid', '请选择不超过 1 GiB 的视频')
        check(isinstance(payload['name'], str) and 0 < len(payload['name']) <= 240
              and re.search(r'\.(mp4|mov)$', payload['name'], re.I)
              and not any(ord(c) < 32 for c in payload['name'])
              and isinstance(payload['series'], str) and len(payload['series']) <= 180
              and type(payload['episode']) is int and 0 <= payload['episode'] <= 100000,
              'name_invalid', '请检查文件名、剧名及集号')
        who = owner(actor)
        with self.lock(who + operation):
            with self.rt.db(True) as db:
                row = db.execute('SELECT * FROM cloud_uploads WHERE owner=? AND operation=?', (who, operation)).fetchone()
                if row:
                    check(row['payload'] == packed(payload), 'operation_conflict', '重新选择的文件信息已变化', 409)
                else:
                    uid, now = ident('au'), time.time()
                    db.execute('''INSERT INTO cloud_uploads
                        (id,owner,operation,payload,status,asset_id,created,updated)
                        VALUES(?,?,?,?,'initializing',?,?,?)''', (uid, who, operation, packed(payload), ident('aa'), now, now))
                    row = db.execute('SELECT * FROM cloud_uploads WHERE id=?', (uid,)).fetchone()
            row = dict(row)
            if row['status'] == 'initializing':
                # Persist the unknown marker before the external write. Retrying
                # a lost response reconciles this exact object key first.
                self.update(row['id'], status='initializing_unknown')
                try:
                    result = self.client.create_multipart_upload(Bucket=self.bucket, Key=self.key(row), ACL='private',
                        ContentType='video/mp4', Metadata={'x-cos-meta-ai-edit-upload': row['id']})
                    check(result.get('UploadId'), 'initialize_unknown', '上传初始化结果待核对', 503)
                    row = self.update(row['id'], status='uploading', remote_id=result['UploadId'])
                except Exception:
                    raise Fault('initialize_unknown', '上传初始化暂未确认，请重新选择同一文件继续', 503) from None
            if row['status'] == 'initializing_unknown':
                uploads = self.client.list_multipart_uploads(Bucket=self.bucket, Prefix=self.key(row), MaxUploads=1000)
                matches = [p for p in uploads.get('Upload', []) if p['Key'] == self.key(row)]
                check(len(matches) == 1, 'initialize_unknown', '上传初始化结果仍待核对，请稍后重试', 503)
                row = self.update(row['id'], status='uploading', remote_id=matches[0]['UploadId'])
            uploaded = self.parts(row) if row['status'] == 'uploading' else []
            if row['status'] in ('completing', 'complete_unknown'):
                row = self.complete(row['id'], actor)
            return self.dto(row, uploaded)

    def parts(self, row):
        result = self.client.list_parts(Bucket=self.bucket, Key=self.key(row), UploadId=row['remote_id'], MaxParts=1000)
        check(str(result.get('IsTruncated', 'false')).lower() != 'true', 'parts_invalid', '分片列表无效', 502)
        return [{'number': int(p['PartNumber']), 'size': int(p['Size']), 'etag': p['ETag']}
                for p in result.get('Part', [])]

    def sign(self, uid, actor, number, body):
        self.configured()
        digest = body.get('md5', '')
        try:
            binary = base64.b64decode(digest, validate=True)
        except Exception:
            binary = b''
        check(len(binary) == 16 and base64.b64encode(binary).decode() == digest,
              'part_digest_invalid', '分片校验值无效')
        with self.lock(uid):
            row = self.row(uid, actor)
            payload = json.loads(row['payload'])
            check(row['status'] == 'uploading' and time.time() - row['created'] < 7 * 86400,
                  'upload_not_writable', '该上传已结束或过期，请刷新状态', 409)
            check(1 <= number <= math.ceil(payload['size'] / PART_BYTES), 'part_invalid', '分片编号无效')
            expected = json.loads(row['parts'])
            check(str(number) not in expected or expected[str(number)] == digest,
                  'part_changed', '所选文件内容与原上传不一致，请选择原文件', 409)
            expected[str(number)] = digest
            self.update(uid, parts=packed(expected))
        size = min(PART_BYTES, payload['size'] - (number - 1) * PART_BYTES)
        headers = {'Content-MD5': digest, 'Content-Type': 'application/octet-stream',
                   'x-cos-psize-min': str(size), 'x-cos-psize-max': str(size)}
        signed = self.client.get_presigned_url(Bucket=self.bucket, Key=self.key(row), Method='PUT', Expired=900,
                    Params={'uploadId': row['remote_id'], 'partNumber': str(number)}, Headers=headers)
        url = urlsplit(signed)
        query = parse_qsl(url.query, keep_blank_values=True)
        headers['Authorization'] = '&'.join(k + '=' + v for k, v in query if k.startswith('q-'))
        check('partnumber' in headers['Authorization'] and 'uploadid' in headers['Authorization'],
              'signature_invalid', '分片凭证生成失败', 503)
        return {'url': urlunsplit((url.scheme, url.netloc, url.path,
                     urlencode([(k, v) for k, v in query if not k.startswith('q-')]), '')), 'headers': headers}

    def head(self, row):
        try:
            return {k.lower(): v for k, v in self.client.head_object(Bucket=self.bucket, Key=self.key(row)).items()}
        except Exception as exc:
            if status_code(exc) == 404:
                return None
            raise

    def accept_object(self, row, head):
        check(head and int(head.get('content-length', 0)) == json.loads(row['payload'])['size']
              and head.get('x-cos-meta-ai-edit-upload') == row['id'] and head.get('etag'),
              'object_invalid', '云端文件大小或上传回执不一致', 409)
        acl = self.client.get_object_acl(Bucket=self.bucket, Key=self.key(row))
        grants = acl.get('AccessControlList', {}).get('Grant', [])
        if isinstance(grants, dict):
            grants = [grants]
        check(grants and all(not p.get('Grantee', {}).get('URI')
              and p.get('Grantee', {}).get('Type', '').lower() != 'group' for p in grants),
              'object_not_private', '素材私有访问校验未通过', 409)
        return self.update(row['id'], status='queued', etag=head['etag'], error='')

    def complete(self, uid, actor):
        self.configured()
        with self.lock(uid):
            row = self.row(uid, actor)
            if row['status'] in ('ready', 'queued', 'verifying'):
                return row
            if row['status'] == 'failed':
                return self.update(uid, status='queued', error='')
            check(row['status'] in ('uploading', 'completing', 'complete_unknown'),
                  'upload_not_ready', '上传初始化未完成', 409)
            head = self.head(row)
            if head:
                return self.accept_object(row, head)
            pieces = sorted(self.parts(row), key=lambda p: p['number'])
            payload, expected = json.loads(row['payload']), json.loads(row['parts'])
            check([p['number'] for p in pieces] == list(range(1, math.ceil(payload['size'] / PART_BYTES) + 1)),
                  'parts_incomplete', '还有分片未完成，请重新选择同一文件续传', 409)
            for p in pieces:
                digest = expected.get(str(p['number']), '')
                check(digest and p['etag'].strip('"').lower() == base64.b64decode(digest).hex()
                      and p['size'] == min(PART_BYTES, payload['size'] - (p['number'] - 1) * PART_BYTES),
                      'part_mismatch', '云端分片内容校验不一致', 409)
            self.update(uid, status='completing')
            try:
                self.client.complete_multipart_upload(Bucket=self.bucket, Key=self.key(row), UploadId=row['remote_id'],
                    MultipartUpload={'Part': [{'PartNumber': p['number'], 'ETag': p['etag']} for p in pieces]},
                    Metadata={'x-cos-forbid-overwrite': 'true'})
                return self.accept_object(row, self.head(row))
            except Exception:
                self.update(uid, status='complete_unknown')
                raise Fault('complete_unknown', '文件合并结果待核对，请重新选择同一文件继续', 503) from None

    def location(self, asset, download=False):
        obj = asset['_cos']
        check(obj['bucket'] == self.bucket and obj['key'].startswith(PREFIX),
              'object_location_invalid', '素材存储位置无效', 409)
        params = {'response-content-disposition': "attachment; filename*=UTF-8''" + quote(asset['name'], safe='')} if download else {}
        return {**obj, 'sha256': asset.get('sha256', ''),
                'url': self.client.get_presigned_url(Bucket=self.bucket, Key=obj['key'], Method='GET', Expired=3600, Params=params)}

    def claim(self):
        self.configured()
        with self.rt.db(True) as db:
            now = time.time()
            row = db.execute("""SELECT * FROM cloud_uploads WHERE status='queued'
                OR (status='verifying' AND expires<?) ORDER BY created LIMIT 1""", (now,)).fetchone()
            if not row:
                return None
            lease = ident('lease')
            db.execute("UPDATE cloud_uploads SET status='verifying',lease=?,expires=?,updated=? WHERE id=?",
                       (lease, now + LEASE_SECONDS, now, row['id']))
        row = dict(row)
        location = self.location({'_cos': {'bucket': self.bucket, 'key': self.key(row),
                            'size': json.loads(row['payload'])['size'], 'etag': row['etag']}})
        return {'id': row['id'], 'lease': lease, 'location': location}

    def worker_update(self, uid, lease, action, body):
        with self.rt.db(True) as db:
            row = self.row(uid, db=db)
            if row['status'] == 'ready' and row['lease'] == lease and action == 'verified':
                asset = db.execute('SELECT sha256 FROM assets WHERE id=?', (row['asset_id'],)).fetchone()
                check(asset and asset['sha256'] == body.get('sha256'), 'receipt_conflict', '上传校验回执冲突', 409)
                return {'ok': True}
            check(row['status'] == 'verifying' and row['lease'] == lease, 'lease_lost', '上传校验已被其他节点接管', 409)
            if action == 'heartbeat':
                db.execute('UPDATE cloud_uploads SET expires=? WHERE id=?', (time.time() + LEASE_SECONDS, uid))
            elif action == 'failed':
                db.execute("UPDATE cloud_uploads SET status='failed',error=? WHERE id=?",
                           ('视频校验未完成，请重新选择同一文件重试；已上传的文件无需重传', uid))
            else:
                digest, meta = body.get('sha256', ''), body.get('metadata', {})
                check(isinstance(digest, str) and re.fullmatch('[a-f0-9]{64}', digest)
                      and isinstance(meta, dict) and type(meta.get('has_audio')) is bool
                      and all(type(meta.get(k)) is int and 0 < meta[k] <= cap for k, cap in
                              [('duration_ms', 86400000), ('width', 16384), ('height', 16384)]),
                      'metadata_invalid', '视频校验回执无效', 422)
                payload = json.loads(row['payload'])
                display = {k: meta[k] for k in ('display_width', 'display_height')
                           if type(meta.get(k)) is int and meta[k] > 0}
                meta = {k: meta[k] for k in ('duration_ms', 'width', 'height', 'has_audio')}
                if len(display) == 2:
                    meta.update(display)
                meta['_cos'] = {'bucket': self.bucket, 'key': self.key(row), 'size': payload['size'], 'etag': row['etag']}
                args = (row['owner'], payload['name'], payload['series'], payload['episode'], digest)
                previous = db.execute('SELECT id FROM assets WHERE owner=? AND name=? AND series=? AND episode=? AND sha256=?', args).fetchone()
                aid = previous['id'] if previous else row['asset_id']
                if not previous:
                    db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)', (aid, *args, packed(meta), time.time()))
                db.execute("UPDATE cloud_uploads SET status='ready',asset_id=?,error='',updated=? WHERE id=?",
                           (aid, time.time(), uid))
        return {'ok': True}

    def dispatch(self, actor, verb, path, body):
        if path == '/uploads' and verb == 'POST':
            return {'item': self.create(actor, body)}
        match = re.fullmatch(r'/uploads/(au_[a-f0-9]{32})(?:/(complete|parts/([1-9][0-9]*)/sign))?', path)
        check(match, 'upload_missing', '上传接口不存在', 404)
        uid, action, number = match.groups()
        if not action and verb == 'GET':
            return {'item': self.dto(self.row(uid, actor))}
        if verb == 'POST' and action == 'complete':
            return {'item': self.dto(self.complete(uid, actor))}
        if verb == 'POST' and number:
            return self.sign(uid, actor, int(number), body)
        raise Fault('method_invalid', '请求方式无效', 405)
