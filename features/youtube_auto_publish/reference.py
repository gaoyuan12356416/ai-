"""Download only verified public drama reference images; never follow redirects."""
import hashlib
import http.client
import io
import ipaddress
import os
import re
import socket
import ssl
import stat
import threading
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PIL import Image
from .source import DEFAULT_HOSTS
from .templates import WorkflowError

REFERENCE_HOSTS = DEFAULT_HOSTS + (
    'static-v1.mydramawave.com', 'static-v2.mydramawave.com',
    'cdn.usrgrow.com', 'ads-cdn.yingliang.tech',
)
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
_DNS_SLOTS = threading.BoundedSemaphore(2)


def normalize_reference_url(value, allowed_hosts=REFERENCE_HOSTS):
    """Known first-party CDN HTTP aliases may upgrade to their HTTPS equivalent."""
    text = str(value or '').strip()
    try:
        if not text or len(text) >= 4096 or re.search(r'[\x00-\x20\x7f\\]', text):
            return ''
        parsed = urlsplit(text)
        host = (parsed.hostname or '').lower()
        if host == 'static.mydramawave.com':
            host = 'static-v1.mydramawave.com'
        if (parsed.scheme not in ('https', 'http') or host not in allowed_hosts
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.port not in (None, 443)):
            return ''
        return urlunsplit(('https', host, parsed.path or '/', parsed.query, ''))
    except (TypeError, ValueError):
        return ''


def _validate_image(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (image.format not in ('JPEG', 'PNG', 'WEBP')
                        or image.width < 64 or image.height < 64
                        or image.width * image.height > 25_000_000
                        or getattr(image, 'n_frames', 1) != 1):
                    raise ValueError('image bounds')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except Exception:
        raise WorkflowError('reference_cover_invalid', '原剧封面不是有效的 JPG、PNG 或 WEBP 图片，请检查剧集资料', 409) from None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the already-checked IP, with normal TLS hostname validation."""
    def __init__(self, host, endpoint, deadline):
        super().__init__(host, 443, timeout=max(.1, deadline-time.monotonic()), context=ssl.create_default_context())
        self.endpoint, self.deadline, self.io_socket = endpoint, deadline, None

    def connect(self):
        family, socktype, protocol, _, address = self.endpoint
        sock = socket.socket(family, socktype, protocol)
        self.io_socket = sock
        try:
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Reference connect deadline')
            sock.settimeout(remaining)
            sock.connect(address)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            self.io_socket = self.sock
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Reference TLS deadline')
            self.sock.settimeout(remaining)
            self.sock.do_handshake()
        except BaseException:
            sock.close()
            if self.sock is not None:
                self.sock.close()
            raise


def _resolve_public(host, deadline):
    """Bound resolver waiting too; a stalled OS lookup cannot pin API workers."""
    remaining = deadline-time.monotonic()
    if remaining <= 0 or not _DNS_SLOTS.acquire(timeout=remaining):
        raise TimeoutError('Reference DNS busy')
    done, result = threading.Event(), []
    def resolve():
        try:
            result.append(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except Exception as exc:
            result.append(exc)
        finally:
            _DNS_SLOTS.release()
            done.set()
    threading.Thread(target=resolve, daemon=True, name='youtube-reference-dns').start()
    if not done.wait(max(0, deadline-time.monotonic())):
        raise TimeoutError('Reference DNS deadline')
    if isinstance(result[0], Exception):
        raise result[0]
    endpoints = result[0]
    if not endpoints or any(not ipaddress.ip_address(endpoint[4][0]).is_global for endpoint in endpoints):
        raise ValueError('Non-public reference host')
    return endpoints


def fetch_reference_cover_factory(*, allowed_hosts=REFERENCE_HOSTS, timeout=15, max_bytes=MAX_REFERENCE_BYTES):
    """Return original bytes, capped in size/time, for the caller to freeze privately."""
    allowed_hosts = tuple(allowed_hosts)
    if not 0 < timeout <= 30 or not 0 < max_bytes <= MAX_REFERENCE_BYTES:
        raise ValueError('Invalid reference download limits')

    def fetch(url):
        normalized = normalize_reference_url(url, allowed_hosts)
        if not normalized:
            raise WorkflowError('reference_cover_url_invalid', '原剧封面地址无效或不在允许的图片域名范围内', 409)
        parsed = urlsplit(normalized)
        deadline = time.monotonic() + timeout
        connection = guard = None
        try:
            endpoints = _resolve_public(parsed.hostname, deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError('Reference deadline')
            # Pin resolution to the validated public IP. No environment proxies,
            # second DNS lookup, credential forwarding or redirect handling.
            connection = _PinnedHTTPSConnection(parsed.hostname, endpoints[0], deadline)
            def stop_at_deadline():
                try:
                    if connection.io_socket is not None:
                        connection.io_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            # Also interrupt slow header/trickle responses, whose internal
            # readline otherwise refreshes a socket timeout for each recv.
            guard = threading.Timer(max(.001, deadline-time.monotonic()), stop_at_deadline)
            guard.daemon = True
            guard.start()
            path = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
            connection.request('GET', path, headers={
                'Accept': 'image/jpeg,image/png,image/webp', 'Accept-Encoding': 'identity',
                'User-Agent': 'YouTubeDramaReference/1.0', 'Connection': 'close',
            })
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError('Reference HTTP status')
            if response.getheader('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise ValueError('Unexpected reference encoding')
            content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type and content_type not in ('image/jpeg', 'image/png', 'image/webp', 'application/octet-stream'):
                raise ValueError('Unexpected reference type')
            content_length = response.getheader('Content-Length')
            if content_length is not None and (not content_length.isdigit() or not 0 < int(content_length) <= max_bytes):
                raise ValueError('Reference size')
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Reference deadline')
                connection.io_socket.settimeout(remaining)
                # read1 performs at most one underlying read, so a slow stream
                # cannot reset an unbounded per-chunk wait indefinitely.
                chunk = response.read1(min(64 * 1024, max_bytes-size+1))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('Reference size')
                chunks.append(chunk)
                if content_length is not None and size == int(content_length):
                    # HTTPResponse closes its file/socket as soon as the known
                    # body length is consumed; don't touch that closed socket.
                    break
            data = b''.join(chunks)
            if not data or (content_length is not None and len(data) != int(content_length)):
                raise ValueError('Incomplete reference')
            _validate_image(data)
            return data
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError('reference_cover_fetch_failed', '原剧封面获取失败，请检查图片地址或稍后重试', 503) from None
        finally:
            if guard is not None:
                guard.cancel()
            if connection is not None:
                connection.close()
    return fetch


def frozen_reference_bytes(root, reference):
    """Only server-issued immutable JPEG IDs may become generator attachments."""
    reference = reference if isinstance(reference, dict) else {}
    asset_id, digest = str(reference.get('asset_id', '')), str(reference.get('sha256', ''))
    if re.fullmatch(r'[a-f0-9]{32}', asset_id) is None or re.fullmatch(r'[a-f0-9]{64}', digest) is None:
        raise WorkflowError('reference_cover_missing', '缺少已冻结的原剧参考封面，请重新创建发布任务', 409)
    root = Path(root).resolve()
    assets = root / 'assets'
    path = assets / (asset_id + '.jpg')
    try:
        if assets.is_symlink() or path.is_symlink() or assets.resolve() != assets or path.resolve().parent != assets:
            raise ValueError('Reference path')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as source:
            meta = os.fstat(source.fileno())
            if not stat.S_ISREG(meta.st_mode) or not 0 < meta.st_size <= 2*1024*1024:
                raise ValueError('Reference size')
            data = source.read(2*1024*1024+1)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Reference changed')
        _validate_image(data)
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'JPEG':
                raise ValueError('Reference must be frozen JPEG')
        return data
    except Exception:
        raise WorkflowError('reference_cover_changed', '原剧参考封面丢失或发生变化，请重新创建发布任务', 409) from None
