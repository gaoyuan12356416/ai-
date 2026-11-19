#!/usr/bin/env python3
"""Compatibility entry point: income and portraits always refresh together."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_tag_revenue.refresh import refresh, DEFAULT_ROOT, WAREHOUSE_ROOT

if __name__ == '__main__':
    print(json.dumps(refresh(DEFAULT_ROOT, WAREHOUSE_ROOT, '/root/drama_material_service/.env'), ensure_ascii=False))
