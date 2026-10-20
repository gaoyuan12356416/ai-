#!/usr/bin/env python3
"""Compatible entrypoint for the existing 11:00 social publisher report timer.

The state directory, date/chat delivery key and run.lock are retained.
Historical correction namespaces are not exposed as a resend option.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.post_daily_report.delivery import DefiniteFailure, DeliveryUnknown
from scripts.social_publisher_daily_report import CHAT_ID, main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DefiniteFailure, DeliveryUnknown) as exc:
        print(json.dumps(dict(status="failed", reason=str(exc))), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(json.dumps(dict(status="failed", reason=type(exc).__name__)), file=sys.stderr)
        raise SystemExit(1)
