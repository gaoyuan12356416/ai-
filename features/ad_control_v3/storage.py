"""Data-disk-only immutable snapshot storage."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import AdControlV3Error


class SafeDataRoot:
    """Validate and operate a runtime root that must not be the system disk.

    Production should keep ``require_distinct_device=True``. Unit tests may
    explicitly disable only that one check while retaining path traversal,
    symlink, permissions and atomic-write coverage.
    """

    def __init__(
        self,
        root: Any,
        *,
        require_distinct_device: bool = True,
        app_root: Optional[Any] = None,
        max_uncompressed_bytes: int = 64 * 1024 * 1024,
        max_compressed_bytes: int = 32 * 1024 * 1024,
        min_free_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        raw = str(root or "").strip()
        if not raw:
            raise AdControlV3Error(
                "data_root_not_configured",
                "AD_CONTROL_V3_DATA_ROOT is required",
                status=503,
            )
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise AdControlV3Error("unsafe_data_root", "data root must be absolute", status=503)
        candidate = path.resolve(strict=False)
        filesystem_root = Path(candidate.anchor).resolve()
        forbidden_descendants = set()
        if os.name != "nt":
            forbidden_descendants.update({Path("/root").resolve(), Path("/tmp").resolve(), Path("/var/tmp").resolve()})
        if candidate == filesystem_root or any(
            candidate == blocked or blocked in candidate.parents for blocked in forbidden_descendants
        ):
            raise AdControlV3Error("unsafe_data_root", "data root is on a forbidden path", status=503)
        if app_root:
            app_path = Path(app_root).resolve()
            if candidate == app_path or app_path in candidate.parents or candidate in app_path.parents:
                raise AdControlV3Error("unsafe_data_root", "data root cannot be inside the application checkout", status=503)
        # Validate existing path components and target filesystem before the
        # first mkdir. An invalid setting must leave the system disk untouched.
        probe = path
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        if not probe.exists():
            raise AdControlV3Error("unsafe_data_root", "data root has no existing parent", status=503)
        component = probe
        while True:
            if component.is_symlink():
                raise AdControlV3Error("unsafe_data_root", "data root cannot traverse a symlink", status=503)
            if component.parent == component:
                break
            component = component.parent
        if require_distinct_device:
            if os.name == "nt":
                system_drive = Path(os.environ.get("SystemDrive", "C:") + "\\").resolve()
                if candidate.drive.lower() == system_drive.drive.lower():
                    raise AdControlV3Error("unsafe_data_root", "data root must use a non-system drive", status=503)
            else:
                if os.stat(probe).st_dev == os.stat("/").st_dev:
                    raise AdControlV3Error("unsafe_data_root", "data root must be a separate mounted device", status=503)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise AdControlV3Error("unsafe_data_root", "data root cannot be a symlink", status=503)
        self.root = path.resolve(strict=True)
        self.max_uncompressed_bytes = max(1, int(max_uncompressed_bytes))
        self.max_compressed_bytes = max(1, int(max_compressed_bytes))
        self.min_free_bytes = max(0, int(min_free_bytes))
        os.chmod(self.root, 0o700)
        for child in ("snapshots", "logs", "run", "spool", "backups", "tmp", "exports", "cache"):
            child_path = self.root / child
            child_path.mkdir(mode=0o700, exist_ok=True)
            if child_path.is_symlink():
                raise AdControlV3Error("unsafe_data_root", "data-root child cannot be a symlink", status=503)
            os.chmod(child_path, 0o700)

    def _resolve_relative(self, relative: str) -> Path:
        text = str(relative or "").replace("\\", "/").strip("/")
        if not text or text.startswith(".") or ".." in text.split("/"):
            raise AdControlV3Error("unsafe_snapshot_path", "invalid snapshot path")
        target = (self.root / text).resolve(strict=False)
        if self.root not in target.parents:
            raise AdControlV3Error("unsafe_snapshot_path", "snapshot path escapes data root")
        for parent in target.parents:
            if parent == self.root:
                break
            if parent.exists() and parent.is_symlink():
                raise AdControlV3Error("unsafe_snapshot_path", "snapshot path traverses a symlink")
        return target

    def write_snapshot(self, category: str, snapshot_id: str, value: Any) -> Dict[str, Any]:
        safe_category = str(category or "").strip().lower()
        safe_id = str(snapshot_id or "").strip()
        if safe_category not in {"preview", "execution", "runner"}:
            raise AdControlV3Error("unsafe_snapshot_path", "invalid snapshot category")
        if not safe_id or not all(char.isalnum() or char in "-_" for char in safe_id):
            raise AdControlV3Error("unsafe_snapshot_path", "invalid snapshot id")
        relative = "snapshots/%s/%s.json.gz" % (safe_category, safe_id)
        target = self._resolve_relative(relative)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        if len(raw) > self.max_uncompressed_bytes:
            raise AdControlV3Error(
                "snapshot_too_large",
                "snapshot exceeds the uncompressed size limit",
                status=413,
                details={"limit": self.max_uncompressed_bytes},
            )
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        if len(compressed) > self.max_compressed_bytes:
            raise AdControlV3Error(
                "snapshot_too_large",
                "snapshot exceeds the compressed size limit",
                status=413,
                details={"limit": self.max_compressed_bytes},
            )
        if shutil.disk_usage(target.parent).free - len(compressed) < self.min_free_bytes:
            raise AdControlV3Error(
                "data_disk_low_space",
                "data disk does not have the required free space",
                status=507,
                details={"minimum_free_bytes": self.min_free_bytes},
            )
        digest = hashlib.sha256(compressed).hexdigest()
        descriptor = None
        temp_name = ""
        try:
            descriptor, temp_name = tempfile.mkstemp(prefix=".%s." % safe_id, suffix=".tmp", dir=str(target.parent))
            os.chmod(temp_name, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(compressed)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            temp_name = ""
            os.chmod(target, 0o600)
            if os.name != "nt":
                directory_fd = os.open(str(target.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        return {
            "relative_path": relative,
            "sha256": digest,
            "byte_size": len(compressed),
        }

    def read_snapshot(self, metadata: Mapping[str, Any]) -> Any:
        relative = str(metadata.get("relative_path") or "")
        expected_hash = str(metadata.get("sha256") or "")
        target = self._resolve_relative(relative)
        if target.is_symlink() or not target.is_file():
            raise AdControlV3Error("snapshot_missing", "snapshot file is missing", status=409)
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise AdControlV3Error("snapshot_hash_mismatch", "snapshot checksum mismatch", status=409)
        try:
            return json.loads(gzip.decompress(raw).decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AdControlV3Error("snapshot_invalid", "snapshot cannot be decoded", status=409) from exc


class MemorySnapshotStore:
    def __init__(self) -> None:
        self.items: Dict[str, Any] = {}

    def write_snapshot(self, category: str, snapshot_id: str, value: Any) -> Dict[str, Any]:
        relative = "memory/%s/%s" % (category, snapshot_id)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        self.items[relative] = json.loads(raw.decode("utf-8"))
        return {
            "relative_path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
        }

    def read_snapshot(self, metadata: Mapping[str, Any]) -> Any:
        relative = str(metadata.get("relative_path") or "")
        if relative not in self.items:
            raise AdControlV3Error("snapshot_missing", "snapshot is missing", status=409)
        return self.items[relative]


def data_root_from_environment(*, app_root: Optional[Any] = None) -> SafeDataRoot:
    return SafeDataRoot(
        os.environ.get("AD_CONTROL_V3_DATA_ROOT", ""),
        require_distinct_device=True,
        app_root=app_root,
    )
