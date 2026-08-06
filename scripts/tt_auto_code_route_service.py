#!/usr/bin/env python3
"""Start the isolated TT automatic-post code-route broker."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.tt_auto_posts.code_broker import serve  # noqa: E402


if __name__ == "__main__":
    serve()
