#!/usr/bin/env python3
"""Independent readiness check: journal-visible failure, no publish or resume."""
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def main():
    try:
        try:
            response = urlopen("http://127.0.0.1:18831/health", timeout=8)
        except HTTPError as error:
            response = error
        with response:
            data = json.loads(response.read(65536))
        automation = data.get("automation", {})
        ready = data.get("ok") is True and automation.get("ready") is True
        print(json.dumps({"ok": ready, "automation": automation}, sort_keys=True))
        return 0 if ready else 1
    except (OSError, ValueError, URLError):
        print('{"ok":false,"error":"tt_auto_health_unavailable"}')
        return 1


if __name__ == "__main__":
    sys.exit(main())
