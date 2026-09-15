#!/usr/bin/env python3
"""Tracked native renderer child. All media paths arrive in a private plan."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.drama_synthesis.native_gpu import main

if __name__ == "__main__":
    raise SystemExit(main())
