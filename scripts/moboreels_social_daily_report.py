#!/usr/bin/env python3
"""Mobo report, called at 10:00 Beijing; never operates publisher queues."""
from datetime import date, datetime, timedelta
from pathlib import Path
import argparse
import json
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.moboreels_library.settings import Settings
from features.moboreels_daily_report.collector import BEIJING, collect
from features.moboreels_daily_report.delivery import SocialDeliveryStore, deliver
from features.moboreels_daily_report.transport import send_card, DeliveryUnknown, DefiniteFailure
from features.moboreels_daily_report.report import CHAT_ID, build_cards, preview_html, size

STATE = '/mnt/data-disk/moboreels-social-daily-report'


def atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date')
    parser.add_argument('--state-dir', default=STATE)
    parser.add_argument('--feishu-config', default='/root/.codex/plugins/feishu/config.json')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--preview', action='store_true')
    mode.add_argument('--send', action='store_true')
    args = parser.parse_args(argv)
    now = datetime.now(BEIJING)
    yesterday = str(now.date() - timedelta(days=1))
    day = date.fromisoformat(args.date or yesterday).isoformat()
    if args.send and (day != yesterday or now.hour < 10):
        raise ValueError('only_previous_day_after_10am')
    os.umask(0o077)
    state = Path(args.state_dir).resolve()
    if not state.is_relative_to(Path('/mnt/data-disk')) or not os.path.ismount('/mnt/data-disk'):
        raise ValueError('mounted_data_disk_required')
    state.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (state / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps(dict(status='already_running')))
            return 0
        store = SocialDeliveryStore(state / 'delivery.sqlite3') if args.send else None
        try:
            batch = None
            if store:
                prior = store.get(day, CHAT_ID)
                if prior and prior['status'] == 'sent':
                    print(json.dumps(dict(status='already_sent', date=day, message_id=prior['message_id'])))
                    return 0
                if prior and prior['status'] in ('sending', 'unknown'):
                    raise DeliveryUnknown('prior_delivery_unconfirmed')
                batch = store.batch(day, CHAT_ID)
            if batch:
                report, cards = batch['report'], batch['cards']
            else:
                report = collect(day, Settings.from_env())
                cards = build_cards(report)
                if store:
                    store.freeze(day, CHAT_ID, report, cards)
            folder = state / ('reports' if args.send else 'previews') / day
            folder.mkdir(parents=True, exist_ok=True)
            for name, value in [('report', report), ('cards', cards)]:
                atomic_write(folder / (name + '.json'), json.dumps(value, ensure_ascii=False, indent=2))
            atomic_write(folder / 'preview.html', preview_html(cards))
            print(json.dumps(dict(status='ready', date=day, chat_id=CHAT_ID, totals=report['totals'],
                quality=report['quality'], card_bytes=[size(c) for c in cards], path=str(folder)), ensure_ascii=False), flush=True)
            if store:
                receipt = deliver(store, day, CHAT_ID, lambda c, u: send_card(args.feishu_config, CHAT_ID, c, u))
                atomic_write(folder / 'receipt.json', json.dumps(receipt, ensure_ascii=False, indent=2))
                print(json.dumps(dict(date=day, **receipt)), flush=True)
            return 0
        finally:
            if store:
                store.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (DeliveryUnknown, DefiniteFailure) as exc:
        print(json.dumps(dict(status='delivery_failed', reason=str(exc))), file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(json.dumps(dict(status='failed', reason=type(exc).__name__)), file=sys.stderr)
        raise SystemExit(1)
