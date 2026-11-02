#!/usr/bin/env python3
"""Run one CPU/GPU node's independent preparation and publication process pools."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.worker import run_node, enroll_node

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--enroll", metavar="PRIVATE_CONFIG_PATH")
    args = parser.parse_args()
    if args.enroll:
        print(json.dumps(enroll_node(args.enroll)))
    else:
        run_node()
