"""Verified, immutable previews for existing library videos without editor receipts."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from fractions import Fraction
from http.client import HTTPConnection
from urllib.parse import urlsplit

import requests

from .source import safe_url
from .direct_media import youtube_download_hosts
from .templates import WorkflowError

SHA = re.compile(r'[a-f0-9]{64}')


def editor_object_sha(url):
    match = re.fullmatch(r'/ai-video-editing/youtube-final/v1/at_[a-f0-9]{32}-([a-f0-9]{64})\.mp4',
                         urlsplit(url).path)
    return match[1] if match else ''


def editor_evidence(material_id, cookie):
    connection = HTTPConnection('127.0.0.1', int(os.getenv('YOUTUBE_REGULAR_EDITOR_PORT', '18983')), timeout=8)
    try:
        path = '/api/ai-video-editing/youtube-material/evidence/' + str(material_id)
        connection.request('GET', path, headers={'Cookie': cookie, 'Accept': 'application/json'})
        response = connection.getresponse()
        raw = response.read(2 * 1024 * 1024 + 1)
        if response.status in (401, 403, 404):
            return None
        if response.status != 200 or len(raw) > 2 * 1024 * 1024:
            raise ValueError()
        item = json.loads(raw)['item']
        if str(item['material_id']) != str(material_id) or not SHA.fullmatch(item['sha256']):
            raise ValueError()
        return item
    except (OSError, KeyError, TypeError, ValueError):
        raise WorkflowError('regular_editor_unavailable', '剪辑定稿资料暂时无法读取，请稍后重试', 503) from None
    finally:
        connection.close()


def dimensions(path):
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-format_whitelist', 'mov,matroska,avi,mpeg,mpegts,flv,asf',
            '-show_entries', 'stream=codec_type,width,height,duration,sample_aspect_ratio:stream_disposition=attached_pic:'
            'stream_tags=rotate:stream_side_data=rotation:format=duration', '-of', 'json', str(path)
        ], check=True, capture_output=True, timeout=45)
        data = json.loads(result.stdout)
        video = next(s for s in data['streams'] if s.get('codec_type') == 'video'
                     and not s.get('disposition', {}).get('attached_pic'))
        w, h = int(video['width']), int(video['height'])
        ratio = str(video.get('sample_aspect_ratio', '')).replace(':', '/')
        if ratio and ratio not in ('N/A', '0/1'):
            w = max(1, round(w * Fraction(ratio)))
        rotation = next((s['rotation'] for s in video.get('side_data_list', []) if 'rotation' in s),
                        video.get('tags', {}).get('rotate', 0))
        if round(float(rotation) / 90) % 2:
            w, h = h, w
        ms = math.ceil(float(data['format']['duration']) * 1000)
        if min(w, h, ms) <= 0:
            raise ValueError()
        return w, h, ms
    except (OSError, subprocess.SubprocessError, StopIteration, ValueError, KeyError, TypeError, ZeroDivisionError):
        raise WorkflowError('regular_media_probe_failed', '无法读取这条视频的实际画幅和时长', 422) from None


def stage_snapshot(material, root, allowed_hosts):
    """The reviewed browser stream reads this exact file, never a mutable CDN response."""
    url = material['url']
    if not safe_url(url, youtube_download_hosts(url, allowed_hosts)):
        raise WorkflowError('regular_invalid_source', '素材的视频地址不可用', 409)
    root.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, stream=True, timeout=(8, 35), allow_redirects=False,
                             headers={'Accept-Encoding': 'identity'}) as response:
                if response.status_code != 200:
                    raise WorkflowError('regular_source_unreadable', '读取素材文件失败，请重试这条素材', 503)
                length = int(response.headers.get('Content-Length') or 0)
                if length and length > shutil.disk_usage(root).free:
                    raise WorkflowError('regular_stage_space', '临时预览空间不足，请稍后重试', 503)
                digest, size = hashlib.sha256(), 0
                with tempfile.NamedTemporaryFile(dir=root, prefix='.probe-', delete=False) as f:
                    temporary = Path(f.name)
                    deadline = time.monotonic() + 7200
                    for chunk in response.iter_content(1024 * 1024):
                        if time.monotonic() >= deadline:
                            raise WorkflowError('regular_source_timeout', '核对素材超时，可单独重试', 503)
                        f.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if not size or length and length != size:
                    raise WorkflowError('regular_source_incomplete', '素材文件未读完整，请重试', 503)
        w, h, ms = dimensions(temporary)
        fingerprint = digest.hexdigest()
        expected = editor_object_sha(url)
        if expected and fingerprint != expected:
            raise WorkflowError('regular_editor_changed', '正式素材文件与定稿版本不符，请核对该条素材', 409)
        destination = root / (fingerprint + '.video')
        os.replace(temporary, destination)
        return dict(sha256=fingerprint, width=w, height=h, duration_ms=ms, size=size,
                    version='', task_id='', evidence=[], snapshot=True, url=url)
    except (OSError, ValueError, requests.RequestException):
        raise WorkflowError('regular_media_unavailable', '未能验证素材文件，请稍后重试', 503) from None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
