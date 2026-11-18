import json
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from .executors import ExecutionError, COSArtifactStore


class SessionClient:
    def __init__(self, config):
        self.config = config
        self.revision, self.client = None, None
        self.lock = threading.Lock()

    def __getattr__(self, name):
        from qcloud_cos import CosConfig, CosS3Client
        path = Path(self.config["credential_file"])
        with self.lock:
            revision = path.stat().st_mtime_ns
            if revision != self.revision:
                session = json.loads(path.read_text())
                if int(session["expires"]) <= time.time() + 10:
                    raise ExecutionError("cos_session_expired")
                creds = session["credentials"]
                self.client = CosS3Client(CosConfig(Region=self.config["region"], SecretId=creds["TmpSecretId"],
                      SecretKey=creds["TmpSecretKey"], Token=creds["Token"], Scheme="https", Timeout=30), retry=0)
                self.revision = revision
            return getattr(self.client, name)


class ObjectStore:
    def __init__(self, client, bucket, prefix, domain):
        self.client = client
        self.config = SimpleNamespace(cos_bucket=bucket, cos_prefix=prefix.strip("/"), cos_domain=domain.rstrip("/"))

    def url(self, key):
        return self.config.cos_domain + "/" + quote(key, safe="/-._~")

    def head(self, key):
        try:
            return {str(k).lower(): v for k,v in self.client.head_object(Bucket=self.config.cos_bucket, Key=key).items()}
        except Exception as exc:
            if COSArtifactStore.status(exc) == 404:
                return None
            raise

    def upload(self, key, local_path, sha256_value, size, content_type="video/mp4", copy_from=None):
        head = self.head(key)
        def matches(info):
            return (info is not None and int(info.get("content-length", -1)) == size
                    and info.get("x-cos-meta-sha256") == sha256_value)
        if head is not None and not matches(head):
            raise ExecutionError("cos_object_conflict", unknown=True)
        if head is None:
            target = {"Bucket": self.config.cos_bucket, "Key": key}
            headers = {"ContentType": content_type, "Metadata": {"x-cos-meta-sha256": sha256_value}}
            if copy_from:
                source, etag = copy_from
                try:
                    self.client.copy_object(**target, **headers, CopySource=source,
                                            CopyStatus="Replaced", CopySourceIfMatch=etag)
                except Exception as exc:
                    print("cos_delivery_copy_fallback", type(exc).__name__, flush=True)
                copied = self.head(key)
                if matches(copied):
                    print("cos_delivery_copy_verified", size, flush=True)
                    return self.url(key)
                if copied is not None:
                    raise ExecutionError("cos_object_conflict", unknown=True)
            with Path(local_path).open("rb") as body:
                if size < 64 * 1024**2:
                    self.client.put_object(**target, **headers, Body=body, EnableMD5=True)
                else:
                    upload = self.client.create_multipart_upload(**target, **headers)["UploadId"]
                    parts = []
                    try:
                        for index, chunk in enumerate(iter(lambda: body.read(16 * 1024**2), b""), 1):
                            result = self.client.upload_part(**target, UploadId=upload, PartNumber=index, Body=chunk, EnableMD5=True)
                            parts.append({"PartNumber": index, "ETag": result["ETag"]})
                        self.client.complete_multipart_upload(**target, UploadId=upload, MultipartUpload={"Part": parts})
                    except Exception:
                        if not matches(self.head(key)):
                            self.client.abort_multipart_upload(**target, UploadId=upload)
                            raise
            if not matches(self.head(key)):
                raise ExecutionError("cos_object_verify_failed")
        return self.url(key)

    def content(self, path, media_type):
        from features.fb_gpu.random_overlay import sha256_file
        sha, size = sha256_file(path)
        key = self.config.cos_prefix + "/assets/" + sha + Path(path).suffix.lower()
        self.upload(key, path, sha, size, media_type)
        return key
