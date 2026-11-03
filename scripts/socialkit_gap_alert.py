#!/usr/bin/env python3
"""CPU scheduled alert CLI. Preview is the default; sending must be explicit."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.socialkit_gap_alert.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
