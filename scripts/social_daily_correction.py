#!/usr/bin/env python3
"""Explicit revision delivery: preserve the original report and reuse frozen receipts."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.social_daily_report.collector import collect
from features.social_daily_report.delivery import SocialDeliveryStore,deliver
from features.social_daily_report.report import build_cards,preview_html,require_complete
from features.post_daily_report.delivery import send_card,DeliveryUnknown
from scripts.social_publisher_daily_report import CHAT_ID
from scripts.post_daily_report import atomic_write,ensure_storage


def corrected_cards(report,chat_id):
    cards=build_cards(report,chat_id)
    for card in cards:
        card['header']['title']['content']='【归属更正】'+card['header']['title']['content']
        card['elements'].insert(0,{'tag':'div','text':{'tag':'lark_md','content':
            '**更正说明：X 素材池、剧目池定时发布已按实际入池人归属。**\n本版替代同日原播报中的人员归属；原始发布与支付记录保留。'}})
    return cards


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',required=True);p.add_argument('--revision',required=True)
    p.add_argument('--state-dir',default='/mnt/data-disk/youtube-publisher-daily-report')
    p.add_argument('--feishu-config',default='/root/.codex/plugins/feishu/config.json')
    p.add_argument('--send',action='store_true')
    args=p.parse_args(argv)
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',args.revision):raise ValueError('invalid_revision')
    from datetime import date
    day=date.fromisoformat(args.date).isoformat()
    state=Path(args.state_dir);ensure_storage(state);os.umask(0o077)
    folder=state/'corrections'/args.revision/day;folder.mkdir(parents=True,exist_ok=True)
    key=CHAT_ID+'/'+args.revision
    handle=(state/'run.lock').open('a')
    try:
        if os.name=='posix':
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        store=SocialDeliveryStore(folder/'delivery.sqlite3')
        try:
            prior=store.get(day,key)
            if prior and prior['status']=='sent':
                print(json.dumps(dict(status='already_sent',message_id=prior['message_id'])));return 0
            if prior and prior['status'] in ('sending','unknown'):raise DeliveryUnknown('prior_correction_unconfirmed')
            frozen=store.batch(day,key)
            if frozen:
                report,cards=frozen['report'],frozen['cards']
            else:
                report=collect(day);require_complete(report)
                cards=corrected_cards(report,CHAT_ID)
                from features.social_daily_report.report import payload_size,SAFE_PAYLOAD_BYTES
                if any(payload_size(c,CHAT_ID)>SAFE_PAYLOAD_BYTES for c in cards):raise ValueError('correction_card_too_large')
                store.freeze(day,key,report,cards)
            require_complete(report)
            for name,value in [('report.json',report),('cards.json',cards)]:atomic_write(folder/name,json.dumps(value,ensure_ascii=False,indent=2))
            atomic_write(folder/'preview.html',preview_html(cards))
            receipt=dict(status='ready')
            if args.send:
                receipt=deliver(store,day,key,lambda card,uuid:send_card(args.feishu_config,CHAT_ID,card,uuid))
                atomic_write(folder/'receipt.json',json.dumps(receipt,ensure_ascii=False,indent=2))
            print(json.dumps(dict(path=str(folder),date=day,totals=report['totals'],**receipt),ensure_ascii=False))
        finally:store.close()
    finally:handle.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
