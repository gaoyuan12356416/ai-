"""Immutable private finished films, uploaded by the GPU and served from COS."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import quote

from .runtime import MAX_BYTES, check, packed, probe

PREFIX = 'ai-video-editing/v1/outputs/'
LOG = logging.getLogger(__name__)


def object_key(tid, digest):
    check(re.fullmatch(r'at_[a-f0-9]{32}', tid or '') and
          re.fullmatch(r'[a-f0-9]{64}', digest or ''), 'output_identity', '成片标识无效', 422)
    return PREFIX + tid + '/' + digest + '.mp4'


def file_digest(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024**2), b''):
            sha.update(block)
    return sha.hexdigest()


def head(client, bucket, tid, digest, size):
    try:
        result = client.head_object(Bucket=bucket, Key=object_key(tid, digest))
    except Exception as exc:
        if getattr(exc, 'get_status_code', lambda: 0)() == 404:
            return None
        raise
    result = {k.lower(): v for k, v in result.items()}
    check(int(result.get('content-length', 0)) == size and
          result.get('x-cos-meta-sha256') == digest and
          result.get('x-cos-meta-ai-edit-task') == tid and result.get('etag'),
          'output_object_changed', 'COS 成片与制作回执不一致', 422)
    return {'bucket': bucket, 'key': object_key(tid, digest), 'size': size, 'etag': result['etag']}


def worker_client():
    from qcloud_cos import CosConfig, CosS3Client

    class ImmutableClient(CosS3Client):
        # SDK 1.9.44 does not forward upload_file's extra headers to Complete.
        def complete_multipart_upload(self, **kwargs):
            kwargs['Metadata'] = {**kwargs.get('Metadata', {}), 'x-cos-forbid-overwrite': 'true'}
            return super().complete_multipart_upload(**kwargs)

    bucket, region = os.environ.get('COS_BUCKET', ''), os.environ.get('COS_REGION', '')
    check(bucket and region == 'ap-hongkong' and os.environ.get('COS_SECRET_ID') and
          os.environ.get('COS_SECRET_KEY'), 'output_cos_config', '成片 COS 配置不完整', 503)
    config = CosConfig(Region=region, SecretId=os.environ['COS_SECRET_ID'],
                       SecretKey=os.environ['COS_SECRET_KEY'], Scheme='https', Timeout=60)
    return ImmutableClient(config, retry=0), bucket


def upload(client, bucket, tid, path, digest):
    """Reconcile a lost COS receipt before repeating the same immutable upload."""
    path = Path(path)
    size = path.stat().st_size
    check(0 < size <= 8 * MAX_BYTES and file_digest(path) == digest,
          'output_file_changed', '本地成片与制作回执不一致', 422)
    meta = probe(path)
    meta['sha256'] = digest
    obj = head(client, bucket, tid, digest, size)
    if obj is None:
        try:
            client.upload_file(Bucket=bucket, Key=object_key(tid, digest), LocalFilePath=str(path),
                               PartSize=16, MAXThread=3, EnableMD5=True, ACL='private',
                               ContentType='video/mp4', Metadata={'x-cos-meta-sha256': digest,
                               'x-cos-meta-ai-edit-task': tid, 'x-cos-forbid-overwrite': 'true'})
        except Exception:
            if head(client, bucket, tid, digest, size) is None:
                raise
        obj = head(client, bucket, tid, digest, size)
    check(obj, 'output_upload_pending', '成片上传回执尚未就绪', 503)
    return {'metadata': meta, 'size': size}


def make_preview(source, target, *, encoder=None):
    """A bounded-bitrate viewing copy; the downloadable master stays unchanged."""
    from .render_worker import run_tool
    source, target = Path(source), Path(target)
    if target.is_file():
        return target
    encoder = encoder or os.getenv('AI_VIDEO_EDITING_ENCODER', 'h264_nvenc')
    codec = (['-c:v','h264_nvenc','-preset','fast','-rc','vbr','-cq','28','-b:v','600k']
             if encoder == 'h264_nvenc' else
             ['-c:v','libx264','-threads','2','-preset','veryfast','-crf','28','-b:v','600k'])
    temporary = target.with_name(target.stem+'.part.mp4')
    try:
        run_tool([os.getenv('AI_VIDEO_EDITING_FFMPEG','ffmpeg'),'-hide_banner','-loglevel','error',
                  '-nostdin','-y','-threads','2','-filter_threads','1','-i',str(source),
                  '-map','0:v:0','-map','0:a:0?',
                  '-vf',"scale='min(480,iw)':'min(854,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                  '-fpsmax','30',*codec,'-maxrate','900k','-bufsize','1800k','-pix_fmt','yuv420p',
                  '-c:a','aac','-b:a','64k','-movflags','+faststart','-map_metadata','-1',str(temporary)])
        actual, original = probe(temporary), probe(source)
        check(actual['has_audio'] and abs(actual['duration_ms']-original['duration_ms']) < 150,
              'preview_mismatch','流畅预览时间轴校验失败',422)
        os.replace(temporary,target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def upload_bundle(client, bucket, tid, path, digest, *, require_preview=False):
    result = upload(client,bucket,tid,path,digest)
    try:
        preview = make_preview(path,Path(path).with_name(Path(path).stem+'.preview.mp4'))
        result['preview'] = upload(client,bucket,tid,preview,file_digest(preview))
    except Exception as exc:
        if require_preview:
            raise
        # The original film is still usable if an optional derivative fails.
        LOG.warning('Finished-film preview unavailable task=%s kind=%s',tid,type(exc).__name__)
    return result


def complete(rt, tid, lease, body, *, migrate=False):
    """Only a worker-authenticated route may call this; never accept arbitrary keys."""
    meta, size = body.get('metadata'), body.get('size')
    check(isinstance(meta, dict) and type(size) is int and 0 < size <= 8 * MAX_BYTES,
          'output_metadata', '成片元数据无效', 422)
    digest = meta.get('sha256', '')
    object_key(tid, digest)
    with rt.db() as db:
        row = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        check(row, 'task_missing', '作品不存在', 404)
        existing = json.loads(row['meta'])
        if migrate or row['status'] == 'completed':
            check(row['status'] == 'completed' and existing.get('sha256') == digest and
                  (migrate or row['lease'] == lease), 'output_receipt_conflict', '成片回执已变化', 409)
            if existing.get('_cos') and (not body.get('preview') or existing.get('_cos_preview')):
                return {'ok': True}
        else:
            rt.leased(db, tid, lease)
        unit = json.loads(row['unit_data'])
        frames = sum((c['end_ms']*30+500)//1000 - (c['start_ms']*30+500)//1000 for c in unit['clips'])
        canvas = unit.get('youtube_regular', {}).get('canvas', [720, 1280])
        check([meta.get('width'), meta.get('height')] == canvas and meta.get('has_audio') is True and
              type(meta.get('duration_ms')) is int and abs(meta['duration_ms']-frames*1000/30) < 150 and
              size <= (8 * MAX_BYTES if unit.get('youtube_regular') else MAX_BYTES),
              'output_mismatch', '成片尺寸、时长或音轨校验失败', 422)
    rt.cloud.configured()
    obj = head(rt.cloud.client, rt.cloud.bucket, tid, digest, size)
    check(obj, 'output_upload_pending', 'COS 成片尚未就绪', 503)
    stored = {k: meta[k] for k in ('width', 'height', 'duration_ms', 'has_audio', 'sha256')}
    stored.update(display_width=meta['width'], display_height=meta['height'], _cos=obj)
    preview = body.get('preview')
    if preview:
        check(isinstance(preview,dict) and isinstance(preview.get('metadata'),dict),
              'preview_metadata','流畅预览回执无效',422)
        pm = preview['metadata'];ps = preview.get('size');pd = pm.get('sha256','')
        check(type(ps) is int and 0 < ps <= MAX_BYTES and
              type(pm.get('width')) is int and 0 < pm['width'] <= 480 and
              type(pm.get('height')) is int and 0 < pm['height'] <= 854 and
              type(pm.get('duration_ms')) is int and abs(pm['duration_ms']-meta['duration_ms']) < 150 and
              pm.get('has_audio') is True and abs(pm['width']/pm['height']-meta['width']/meta['height']) < .01,
              'preview_mismatch','流畅预览与成片不一致',422)
        pobj = head(rt.cloud.client,rt.cloud.bucket,tid,pd,ps)
        check(pobj,'preview_pending','流畅预览上传尚未确认',503)
        stored['_cos_preview'] = {**pobj,'sha256':pd}
    with rt.db(True) as db:
        current = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        if current['status'] == 'completed':
            old = json.loads(current['meta'])
            check(old.get('sha256') == digest and (migrate or current['lease'] == lease),
                  'output_receipt_conflict', '成片回执已变化', 409)
            stored = {**old,**stored}
        else:
            check(not migrate, 'output_receipt_conflict', '仅可迁移已完成成片', 409)
            rt.leased(db, tid, lease)
        db.execute("UPDATE tasks SET status='completed',meta=? WHERE id=?", (packed(stored), tid))
    return {'ok': True}


def record(rt, tid):
    with rt.db() as db:
        row = db.execute("SELECT meta FROM tasks WHERE id=? AND status='completed'", (tid,)).fetchone()
    check(row, 'output_missing', '成片尚未就绪', 404)
    return json.loads(row['meta'])


def location(rt, tid, name='', download=False, method='GET', preview=False):
    meta = record(rt, tid)
    obj = (meta.get('_cos_preview') if preview and not download else None) or meta.get('_cos')
    if not obj:
        return None
    digest = obj.get('sha256',meta['sha256'])
    check(obj['bucket'] == rt.cloud.bucket and obj['key'] == object_key(tid, digest),
          'output_location', '成片存储位置不符', 409)
    params = {'response-content-disposition': "attachment; filename*=UTF-8''" + quote(name, safe='')} if download else {}
    return {**obj, 'sha256': digest, 'url': rt.cloud.client.get_presigned_url(
        Bucket=obj['bucket'], Key=obj['key'], Method=method, Expired=3600, Params=params)}


def local_file(rt, tid):
    """Legacy CPU consumers materialize only when actually requested."""
    target = rt.root/'outputs'/(tid+'.mp4')
    if target.is_file():
        return target
    loc = location(rt, tid)
    check(loc, 'output_missing', '成片文件暂不可用', 404)
    from .cloud_media import download
    with rt.cloud.lock('output:' + tid):
        if not target.is_file():
            temporary = target.with_suffix('.downloading')
            download(loc, temporary, output=True)
            os.replace(temporary, target)
    return target


def file_size(rt, tid):
    meta = record(rt, tid)
    return meta['_cos']['size'] if meta.get('_cos') else local_file(rt, tid).stat().st_size
