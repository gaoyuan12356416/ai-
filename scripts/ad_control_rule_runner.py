#!/usr/bin/env python3
"""Retired scheduler entry point. It never opens a database or contacts Meta."""
import json

if __name__ == "__main__":
    print(json.dumps({"ok": True, "status": "retired", "meta_writes": 0}))
