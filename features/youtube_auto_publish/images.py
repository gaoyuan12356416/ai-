"""Strict raster decoding shared by generated, uploaded and reference covers."""
import hashlib
import io
import struct
import warnings
import zlib

from PIL import Image, ImageOps
from .templates import WorkflowError

MAX_PIXELS = 25_000_000
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class _ImageProblem(ValueError):
    pass


def _fail(kind):
    raise _ImageProblem(kind)


def _scanlines(chunks, width, height, bits_per_pixel, interlace):
    """Check the real zlib stream against every declared (including Adam7) row."""
    passes = [(width, height)] if interlace == 0 else [
        (max(0, (width-x+dx-1)//dx), max(0, (height-y+dy-1)//dy))
        for x,y,dx,dy in ((0,0,8,8),(4,0,8,8),(0,4,4,8),(2,0,4,4),(0,2,2,4),(1,0,2,2),(0,1,1,2))]
    rows = [((w*bits_per_pixel+7)//8+1, h) for w,h in passes if w and h]
    expected = sum(stride*count for stride,count in rows)
    strides = (stride for stride,count in rows for _ in range(count))
    position = next_filter = 0

    def consume(block):
        nonlocal position, next_filter
        end = position+len(block)
        if end > expected:
            _fail('corrupt')
        while next_filter < end:
            if block[next_filter-position] > 4:
                _fail('corrupt')
            next_filter += next(strides)
        position = end

    decoder = zlib.decompressobj()
    for chunk in chunks:
        for offset in range(0, len(chunk), 65536):
            pending = chunk[offset:offset+65536]
            while pending:
                block = decoder.decompress(pending, 65536)
                consume(block)
                if decoder.unused_data:
                    _fail('corrupt')
                pending = decoder.unconsumed_tail
    consume(decoder.flush())
    if not decoder.eof or decoder.unused_data or position != expected or next_filter != expected:
        _fail('corrupt')


def _validate_png(data):
    position, seen, image_chunks = 8, set(), []
    width = height = depth = color = interlace = 0
    idat_finished = ended = False
    while position < len(data):
        if len(data)-position < 12:
            _fail('corrupt')
        length = struct.unpack_from('>I', data, position)[0]
        kind = data[position+4:position+8]
        end = position+length+12
        if end > len(data) or not all(65 <= char <= 90 or 97 <= char <= 122 for char in kind) or kind[2]&32:
            _fail('corrupt')
        payload = data[position+8:position+8+length]
        crc = struct.unpack_from('>I', data, position+8+length)[0]
        if zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff != crc:
            _fail('corrupt')
        if not seen and kind != b'IHDR':
            _fail('corrupt')
        if kind == b'IHDR':
            if seen or length != 13:
                _fail('corrupt')
            width,height,depth,color,compression,filter_method,interlace = struct.unpack('>IIBBBBB', payload)
            if not width or not height:
                _fail('corrupt')
            if width*height > MAX_PIXELS:
                _fail('dimensions')
            depths = {0:(1,2,4,8,16), 2:(8,16), 3:(1,2,4,8), 4:(8,16), 6:(8,16)}
            if depth not in depths.get(color, ()) or compression or filter_method or interlace not in (0,1):
                _fail('corrupt')
        elif kind in (b'acTL', b'fcTL', b'fdAT'):
            _fail('format')
        elif kind == b'PLTE':
            if kind in seen or b'IDAT' in seen or not length or length%3 or length > 768 or color in (0,4):
                _fail('corrupt')
            if color == 3 and length//3 > 2**depth:
                _fail('corrupt')
        elif kind == b'IDAT':
            if idat_finished or (color == 3 and b'PLTE' not in seen):
                _fail('corrupt')
            image_chunks.append(payload)
        elif kind == b'IEND':
            if length or not image_chunks or end != len(data):
                _fail('corrupt')
            ended = True
            break
        elif not kind[0]&32:
            # Unknown critical chunks cannot be silently ignored.
            _fail('format')
        if b'IDAT' in seen and kind != b'IDAT':
            idat_finished = True
        seen.add(kind)
        position = end
    if not ended:
        _fail('corrupt')
    channels = {0:1, 2:3, 3:1, 4:2, 6:4}[color]
    _scanlines(image_chunks, width, height, depth*channels, interlace)


def decode_cover(data, *, generated=False, reference=False):
    """Decode actual pixels to detached RGB; never repair malformed image data."""
    prefix = 'reference_cover' if reference else 'generated_cover' if generated else 'cover'
    subject = '原剧参考封面' if reference else 'AI 生成的封面' if generated else '上传的封面'
    status = 409 if reference else 503 if generated else 400
    formats = ('JPEG','PNG','WEBP') if reference else ('JPEG','PNG')
    try:
        if not isinstance(data, bytes) or not data:
            _fail('corrupt')
        if data.startswith(PNG_SIGNATURE):
            _validate_png(data)
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in formats or getattr(image, 'n_frames', 1) != 1:
                    _fail('format')
                if image.width*image.height > MAX_PIXELS:
                    _fail('dimensions')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                image = ImageOps.exif_transpose(image)
                if image.width < (64 if reference else 320) or image.height < (64 if reference else 180):
                    _fail('dimensions')
                if generated and abs(image.width/image.height-16/9) > .03:
                    _fail('ratio')
                if image.mode in ('RGBA','LA') or 'transparency' in image.info:
                    rgba = image.convert('RGBA')
                    result = Image.new('RGB', rgba.size, 'white')
                    result.paste(rgba, mask=rgba.getchannel('A'))
                else:
                    result = image.convert('RGB')
                result.info.clear()
                return result
    except _ImageProblem as error:
        kind = str(error)
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        kind = 'dimensions'
    except Image.UnidentifiedImageError:
        kind = 'corrupt' if data and (data.startswith(PNG_SIGNATURE) or data.startswith(b'\xff\xd8') or data.startswith(b'RIFF')) else 'format'
    except Exception:
        kind = 'corrupt'
    message = {
        'corrupt':subject+'文件损坏或数据不完整，无法读取；请重新生成或更换图片。',
        'format':subject+'格式不支持或包含多帧；请使用单张 '+('JPG、PNG 或 WEBP' if reference else 'JPG 或 PNG')+' 图片。',
        'dimensions':subject+'尺寸不符合要求；像素总量不能超过 2500 万，宽高至少为 '+('64×64' if reference else '320×180')+'。',
        'ratio':subject+'宽高比例不符合 16:9，请按 16:9 要求重新生成。',
    }[kind]
    raise WorkflowError(prefix+'_'+kind, message, status) from None


def normalize_generated_cover(data):
    """Crop decoded pixels to exact 16:9, allowing only small balanced edge loss.

    Keep decode_cover read-only: previews and historical approved assets must not
    change. This helper is called only for a newly completed AI generation.
    """
    image = decode_cover(data, generated=True)
    width, height = image.size
    scale = min(width // 16, height // 9)
    target_width, target_height = 16 * scale, 9 * scale
    removed_width, removed_height = width-target_width, height-target_height
    if (target_width < 320 or target_height < 180 or
            removed_width * 100 > width * 2 or removed_height * 100 > height * 2 or
            (width * height - target_width * target_height) * 100 > width * height * 3):
        raise WorkflowError('generated_cover_crop_excessive',
            'AI 封面裁成 16:9 需要移除过多画面，可能影响人物或标题；请重新生成。', 503)
    left, top = removed_width // 2, removed_height // 2
    box = (left, top, left + target_width, top + target_height)
    cropped = bool(removed_width or removed_height)
    result = data
    if cropped:
        out = io.BytesIO()
        image.crop(box).save(out, format='PNG')
        result = out.getvalue()
    audit = {'policy': 'center_crop_16_9_v1', 'cropped': cropped,
             'source_size': [width, height], 'output_size': [target_width, target_height],
             'crop_box': list(box), 'removed_edges': [left, top, removed_width-left, removed_height-top],
             'source_sha256': hashlib.sha256(data).hexdigest(),
             'output_sha256': hashlib.sha256(result).hexdigest()}
    return result, audit
