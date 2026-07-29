#!/usr/bin/env python3
"""Run the loopback-only TT GPU preparation and publishing sidecar."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.tt_gpu.worker import serve  # noqa: E402


if __name__ == "__main__":
    serve()
