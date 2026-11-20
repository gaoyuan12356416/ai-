"""Private Hong Kong COS reads, shared by the API and the Hong Kong worker."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

from .runtime import MAX_BYTES, check


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url):
    value = urlsplit(url)
    check(value.scheme == 'https' and not value.username and not value.password
          and value.port in (None, 443) and not value.fragment
          and re.fullmatch(r'[a-z0-9-]+-\d+\.cos\.ap-hongkong\.myqcloud\.com', value.hostname or '')
          and value.path.startswith('/ai-video-editing/v1/uploads/'),
          'cloud_location_invalid', '素材存储地址无效', 502)
    return url


def download(location, target, stopped=None):
    """Never forward the internal worker token to COS; verify the frozen object."""
    url = validate_url(location['url'])
    size, etag = location['size'], location['etag']
    check(type(size) is int and 0 < size <= MAX_BYTES and isinstance(etag, str) and etag,
          'cloud_identity_invalid', '素材存储回执无效', 502)
    target = Path(target)
    check(shutil.disk_usage(target.parent).free > size + 128 * 1024**2,
          'media_disk_full', '媒体工作盘空间不足', 503)
    started = time.monotonic()
    digest, count = hashlib.sha256(), 0
    try:
        request = Request(url, headers={'If-Match': etag, 'Accept-Encoding': 'identity'})
        with build_opener(NoRedirects()).open(request, timeout=60) as response:
            check(response.status == 200 and int(response.headers.get('Content-Length', 0)) == size
                  and response.headers.get('ETag', '').strip('"') == etag.strip('"'),
                  'cloud_identity_changed', '素材存储版本已变化', 409)
            with target.open('wb') as out:
                while True:
                    check(not (stopped and stopped.is_set()) and time.monotonic() - started < 1800,
                          'media_download_interrupted', '素材读取中断，请重试', 503)
                    block = response.read(1024**2)
                    if not block:
                        break
                    count += len(block)
                    check(count <= size, 'cloud_size_changed', '素材字节数不符', 409)
                    digest.update(block)
                    out.write(block)
        actual = digest.hexdigest()
        check(count == size and (not location.get('sha256') or actual == location['sha256']),
              'cloud_digest_changed', '素材内容校验不一致', 409)
        return actual
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def materialize(runtime, asset):
    path = Path(asset['path'])
    if path.is_file():
        return asset
    check(asset.get('_cos') and getattr(runtime, 'cloud', None),
          'media_missing', '素材文件暂不可读取', 404)
    with runtime.cloud.lock(asset['id']):
        if not path.is_file():
            temporary = path.with_suffix('.downloading')
            download(runtime.cloud.location(asset), temporary)
            os.replace(temporary, path)
    return asset
