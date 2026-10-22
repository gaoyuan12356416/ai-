"""Exercise XLSX parsing as the production service identity, without DB/API writes."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys


def check_as_service_user(root):
    """Root's private site-packages must never satisfy the deployment check."""
    user = subprocess.check_output([
        "systemctl", "show", "fb-auto-post-service.service", "-p", "User", "--value"
    ], text=True).strip()
    if user != "fb-auto-post":
        raise RuntimeError("Unexpected FB service identity; inspect before deployment")
    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONNOUSERSITE"}}
    # Match the service's normal site initialization. On this RHEL Python,
    # PYTHONNOUSERSITE also removes the shared /usr/local site-packages path.
    # runuser sets HOME=/nonexistent, so root's private user site stays excluded.
    subprocess.run([
        "runuser", "-u", user, "--", "/usr/bin/python3", "-", str(Path(root).resolve())
    ], input=Path(__file__).read_text(encoding="utf-8"), text=True,
        cwd=root, env=env, check=True, timeout=30)


def check_parser(root):
    sys.path.insert(0, str(Path(root).resolve()))
    import openpyxl
    import et_xmlfile
    from features.fb_video_publish.excel import HEADERS, read_workbook

    workbook = openpyxl.Workbook()
    workbook.active.append(HEADERS)
    # Text IDs exceed Excel's numeric precision; exercise production defaults.
    page_id = "12345678901234567"
    workbook.active.append(["Dramawave", page_id, "", "", "21", "parser smoke", "", "title"])
    data = io.BytesIO()
    workbook.save(data)
    workbook.close()
    _, rows, errors, total = read_workbook("smoke.xlsx", base64.b64encode(data.getvalue()).decode())
    if (total != 1 or errors or len(rows) != 1 or rows[0]["page_id"] != page_id
            or rows[0]["processing_mode"] != "original" or rows[0]["credential_mode"] != "page_pool"):
        raise RuntimeError("Excel runtime parser smoke check failed")
    print(json.dumps({"excel_parser_ok": True, "rows": total,
                      "uid": os.getuid() if hasattr(os, "getuid") else None,
                      "python": sys.executable, "openpyxl": openpyxl.__version__,
                      "openpyxl_path": openpyxl.__file__, "et_xmlfile_path": et_xmlfile.__file__}))


if __name__ == "__main__":
    check_parser(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
