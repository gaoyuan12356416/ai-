#!/usr/bin/env python3
"""Synchronize receipts without calling any publication, upload or token API."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.x_tag_publish import receipt_settlement as settlement


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["init","scope","observe"])
    ap.add_argument("--posts",required=True);ap.add_argument("--auto",required=True);ap.add_argument("--dispatch",required=True)
    ap.add_argument("--accounts");ap.add_argument("--scope");ap.add_argument("--evidence");ap.add_argument("--since")
    ap.add_argument("--output")
    args=ap.parse_args()
    with closing(settlement.open_ledgers(args.posts,args.auto,args.dispatch)) as db:
        if args.command=="init":
            settlement.initialize(db);return 0
        if args.command=="scope":
            items=json.loads(Path(args.scope).read_text(encoding="utf-8-sig"))
            evidence=json.loads(Path(args.evidence).read_text(encoding="utf-8-sig"))
            if len({(i["task"],i["queue"]) for i in items})!=len(items): raise ValueError("duplicate scope")
            result=settlement.apply_items(db,items,evidence)
        else:
            if not args.since or not args.accounts: raise ValueError("activation timestamp and account metadata source required")
            with closing(sqlite3.connect(Path(args.accounts).resolve(strict=True).as_uri()+"?mode=ro",uri=True,timeout=3)) as accounts:
                db.execute("BEGIN IMMEDIATE")
                try:
                    waits=settlement.refresh_waits(db,accounts)
                    db.commit()
                except BaseException:
                    db.rollback();raise
            result=settlement.apply_items(db,settlement.candidates(db,args.since))
            result["waits"]=waits
        if args.output:
            Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(dict(settled=len(result["settled"]),already=len(result["already"]),
                              review=result["review"][:12],review_count=len(result["review"]),waits=result.get("waits")),ensure_ascii=False))
        return int(args.command=="scope" and bool(result["review"]))


if __name__=="__main__":
    sys.exit(main())
