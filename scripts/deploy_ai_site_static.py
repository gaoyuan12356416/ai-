"""Install only reviewed Nginx files, with baseline fences and automatic rollback."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen


def atomic(path, data, mode=0o644):
    fd, temporary = tempfile.mkstemp(prefix=".ai-site-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args()
    backup = args.backup.resolve()
    assert str(backup).startswith("/mnt/data-disk/ai-site-performance-20261008/backup-")
    uuid = subprocess.check_output(["findmnt", "-no", "UUID", "/mnt/data-disk"], text=True).strip()
    assert uuid == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    assert shutil.disk_usage("/mnt/data-disk").free > 1024**3
    repo = Path(__file__).resolve().parents[1]
    original = Path("/etc/nginx/default.d/drama-material-api.conf")
    expected = json.loads((backup / "manifest.json").read_text())["files"][str(original)]["sha256"]
    assert hashlib.sha256(original.read_bytes()).hexdigest() == expected, "Nginx baseline changed"
    files = {
        original: repo / "deploy/nginx/drama-material-api.conf",
        Path("/etc/nginx/conf.d/ai-site-cache-policy.conf"): repo / "deploy/nginx/ai-site-cache-policy.conf",
        Path("/etc/nginx/default.d/ai-site-performance.conf"): repo / "deploy/nginx/ai-site-performance.conf",
    }
    before = {path: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
              for path in files}
    try:
        for target, source in files.items():
            atomic(target, source.read_bytes())
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        expected_html = hashlib.sha256(Path("/usr/share/nginx/html/index.html").read_bytes()).hexdigest()
        for attempt in range(10):
            request = Request("http://127.0.0.1/", headers={"Host": "ai.yingliangads.com", "Accept-Encoding": "gzip"})
            with urlopen(request, timeout=5) as response:
                encoding = response.headers.get("Content-Encoding")
                data = response.read()
                if response.status == 200 and encoding == "gzip":
                    assert hashlib.sha256(gzip.decompress(data)).hexdigest() == expected_html
                    break
            time.sleep(.5)
        else:
            raise RuntimeError("new Nginx workers did not serve compressed HTML")
        with urlopen(Request("http://127.0.0.1/quick-nav.js", headers={"Host": "ai.yingliangads.com"}), timeout=5) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "public, max-age=300, must-revalidate"
        report = {"status": "deployed", "html_gzip_bytes": len(data), "html_sha256": expected_html,
                  "files": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
        (backup.parent / "static-deployment.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    except Exception:
        for path, value in before.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, value[0], value[1])
        subprocess.run(["nginx", "-t"], check=True)
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
        raise


if __name__ == "__main__":
    main()
