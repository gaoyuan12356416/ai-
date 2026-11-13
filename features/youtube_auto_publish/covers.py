"""Private conditional cover responses and bounded, in-memory list thumbnails."""
from collections import OrderedDict
import hashlib
import io
import threading

from PIL import Image, ImageOps

from .templates import WorkflowError

_THUMBNAILS = OrderedDict()
_LOCK = threading.Lock()
_DECODERS = threading.BoundedSemaphore(2)
_VARIANT = 'thumb-160x90-v1'


def _thumbnail(data, sha):
    with _LOCK:
        if sha in _THUMBNAILS:
            _THUMBNAILS.move_to_end(sha)
            return _THUMBNAILS[sha]
    with _DECODERS:
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError('image too large')
                image = ImageOps.exif_transpose(image).convert('RGB')
                image.thumbnail((160, 90), Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS)
                output = io.BytesIO()
                image.save(output, 'JPEG', quality=75, optimize=True)
                result = output.getvalue()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise WorkflowError('cover_unavailable', '封面暂不可用', 409) from None
    with _LOCK:
        _THUMBNAILS[sha] = result
        _THUMBNAILS.move_to_end(sha)
        while len(_THUMBNAILS) > 128:
            _THUMBNAILS.popitem(last=False)
    return result


def cover_response(asset, *, thumbnail=False, if_none_match=''):
    """Caller MUST authorize via service.asset before considering a cached 304.

    Recheck original bytes even on cache hits, preserving immutable-asset guards.
    Private no-cache permits storage but requires authorization on every reuse.
    """
    with open(asset['path'], 'rb') as stream:
        original = stream.read(2 * 1024 * 1024 + 1)
    if len(original) > 2 * 1024 * 1024 or hashlib.sha256(original).hexdigest() != asset['sha256']:
        raise WorkflowError('cover_changed', '封面文件已变化，请重新上传', 409)
    tag = '"' + asset['sha256'] + ('-' + _VARIANT if thumbnail else '') + '"'
    headers = {'Cache-Control': 'private, no-cache, max-age=0, must-revalidate',
               'ETag': tag, 'Vary': 'Cookie', 'X-Content-Type-Options': 'nosniff'}
    tags = [value.strip().removeprefix('W/') for value in str(if_none_match).split(',')]
    if tag in tags or '*' in tags:
        return 304, headers, b''
    data = _thumbnail(original, asset['sha256']) if thumbnail else original
    headers.update({'Content-Type': 'image/jpeg', 'Content-Length': str(len(data))})
    return 200, headers, data
