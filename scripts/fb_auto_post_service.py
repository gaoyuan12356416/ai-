#!/usr/bin/env python3
"""Entrypoint for the loopback FB Page automatic publishing sidecar."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.fb_auto_posts.service import main


if __name__ == "__main__":
    main()
