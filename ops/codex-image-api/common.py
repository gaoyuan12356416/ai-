import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, quote

from store import ApiError


def env_file(path):
    result = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    return result


class Config:
    def __init__(self):
        self.root = Path(os.environ.get("IMAGE_API_STATE", "/mnt/data-disk/codex-image-api"))
        self.backend_env = Path(os.environ.get("IMAGE_API_BACKEND_ENV", "/root/drama_material_service/.env"))
        self.port = int(os.environ.get("IMAGE_API_PORT", "18853"))
        self.codex_bin = os.environ.get("IMAGE_API_CODEX_BIN", "/usr/bin/codex")
        self.model = os.environ.get("IMAGE_API_MODEL", "gpt-6.1-sol")
        self.reasoning = os.environ.get("IMAGE_API_REASONING", "low")
        self.timeout = int(os.environ.get("IMAGE_API_GENERATION_TIMEOUT", "900"))
        self.source_home = Path(os.environ.get("IMAGE_API_CODEX_SOURCE_HOME", "/root/.codex"))
        self.runner_user = "codex-image-runner"
        self.body_limit = 1024 * 1024

    def tokens(self):
        # Read the existing owning config, so rotation needs no duplicate secret.
        source = env_file(self.backend_env)
        raw = source.get("DRAMA_SCREENSHOT_API_TOKENS", "")
        tokens = re.split(r"[\s,;]+", raw) if raw else []
        tokens.append(source.get("DRAMA_SCREENSHOT_API_TOKEN", ""))
        return [t for t in tokens if t]


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        os.chmod(tmp, mode)
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def public_target(url, resolver=socket.getaddrinfo):
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            raise ValueError()
        if any(ord(c) < 32 for c in url):
            raise ValueError()
        port = p.port or (443 if p.scheme == "https" else 80)
        addresses = list(dict.fromkeys(a[4][0] for a in resolver(p.hostname, port, type=socket.SOCK_STREAM)))
        if not addresses:
            raise OSError("no address")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast or (getattr(ip, "ipv4_mapped", None) and not ip.ipv4_mapped.is_global):
                raise ApiError("unsafe_image_url", "图片 URL 不允许访问内网或特殊地址")
        return p, port, addresses[0]
    except ApiError:
        raise
    except (ValueError, TypeError):
        raise ApiError("invalid_image_url", "图片 URL 必须是公开 HTTP/HTTPS 链接")


def download_once(url, destination, max_bytes=32 * 1024 * 1024):
    for redirect in range(6):
        p, port, address = public_target(url)
        cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
        kwargs = {"context": ssl.create_default_context()} if p.scheme == "https" else {}
        conn = cls(p.hostname, port, timeout=45, **kwargs)
        # Pin the verified IP for the actual socket, retaining hostname SNI/TLS validation.
        conn._create_connection = lambda endpoint, timeout, source_address=None: socket.create_connection((address, port), timeout, source_address)
        try:
            path = quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~") + (("?" + p.query) if p.query else "")
            conn.request("GET", path, headers={"User-Agent": "AI-Image-API/1.0", "Accept": "image/*"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ApiError("download_failed", "图片重定向缺少目标")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                if response.status == 429 or response.status >= 500:
                    raise OSError("temporary upstream failure")
                raise ApiError("download_failed", "图片下载 HTTP %d" % response.status)
            length = response.getheader("Content-Length")
            if length and int(length) > max_bytes:
                raise ApiError("image_too_large", "单张参考图不能超过 32 MiB")
            total = 0
            deadline = time.monotonic() + 180
            with open(destination, "wb") as out:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ApiError("image_too_large", "单张参考图不能超过 32 MiB")
                    if time.monotonic() > deadline:
                        raise OSError("download deadline exceeded")
                    out.write(chunk)
            return
        finally:
            conn.close()
    raise ApiError("download_failed", "图片重定向次数过多")


def checked_image(path):
    from PIL import Image
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as im:
                if im.format not in ("JPEG", "PNG", "WEBP") or getattr(im, "n_frames", 1) != 1:
                    raise ValueError("unsupported image")
                im.verify()
            with Image.open(path) as im:
                im.load()
                return im.size
    except Exception:
        raise ApiError("invalid_image", "图片必须是有效的单帧 PNG、JPEG 或 WebP")


def download_image(url, path):
    for attempt in range(3):
        try:
            download_once(url, path)
            checked_image(path)
            return
        except ApiError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            if attempt == 2:
                raise ApiError("download_failed", "图片下载失败，已尝试三次")
            time.sleep(2 ** attempt)


class CosUploader:
    def __init__(self, config):
        from qcloud_cos import CosConfig, CosS3Client
        values = env_file(config.backend_env)
        required = ("COS_SECRET_ID", "COS_SECRET_KEY", "COS_BUCKET", "COS_REGION", "COS_DOMAIN")
        if not all(values.get(k) for k in required):
            raise ApiError("cos_not_configured", "COS 配置缺失", 503)
        self.bucket = values["COS_BUCKET"]
        self.base = "https://" + re.sub(r"^https?://", "", values["COS_DOMAIN"]).rstrip("/")
        self.prefix = values.get("COS_PREFIX", "drama-materials").strip("/") + "/image-generation"
        self.client = CosS3Client(CosConfig(Region=values["COS_REGION"], SecretId=values["COS_SECRET_ID"], SecretKey=values["COS_SECRET_KEY"], Timeout=60), retry=0)

    def upload(self, row, path):
        key = "%s/%s/%04d-%04d.png" % (self.prefix, row["task_id"], row["item_index"], row["image_index"])
        digest, size = sha256(path), Path(path).stat().st_size
        for attempt in range(3):
            try:
                try:
                    head = self.client.head_object(Bucket=self.bucket, Key=key)
                except Exception as exc:
                    if str(getattr(exc, "get_status_code", lambda: 0)()) == "404":
                        head = None
                    else:
                        raise
                if head is not None:
                    if head.get("x-cos-meta-sha256") == digest and int(head.get("Content-Length", -1)) == size:
                        return self.base + "/" + key
                    raise ApiError("cos_object_conflict", "目标 COS 对象与当前图片不一致，已保留原对象")
                with open(path, "rb") as image:
                    self.client.put_object(Bucket=self.bucket, Key=key, Body=image, ContentType="image/png", Metadata={"sha256": digest}, EnableMD5=True)
                head = self.client.head_object(Bucket=self.bucket, Key=key)
                if head.get("x-cos-meta-sha256") == digest and int(head.get("Content-Length", -1)) == size:
                    return self.base + "/" + key
            except ApiError:
                raise
            except Exception:
                pass
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise ApiError("cos_upload_failed", "COS 上传或校验失败，已保留生成图片")
