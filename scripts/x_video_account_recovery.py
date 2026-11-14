#!/usr/bin/env python3
"""One bounded maintenance pass; default is a dry run. Never posts to X."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.x_video_publish.account_recovery import sweep

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='/var/lib/x-post-automation/accounts.sqlite3')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--batch-id',type=int)
    args=parser.parse_args()
    print(json.dumps(sweep(args.db,apply=args.apply,batch_id=args.batch_id),ensure_ascii=False))
