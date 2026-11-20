"""Record actual recheck reasons for untouched slots retained by English recovery."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--audit', required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    audit_path = Path(args.audit)
    audit = json.loads(audit_path.read_text())
    assert audit['applied'] and 0 <= time.time() - audit_path.stat().st_mtime < 900
    sys.path.insert(0, '/opt/fb-auto-post/current')
    from features.fb_auto_posts.skipped_recovery import untouched, REASON
    target = Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
    changed = []
    with sqlite3.connect(target, timeout=30) as c:
        c.row_factory = sqlite3.Row
        c.execute('BEGIN IMMEDIATE')
        for run in audit['runs']:
            for item in run['retained']:
                if item['reason'] not in {'page_not_eligible', 'no_current_eligible_unpublished_english_material'}:
                    continue
                task = dict(c.execute('SELECT * FROM fb_auto_task WHERE id=?', (item['id'],)).fetchone())
                page = c.execute('SELECT * FROM fb_auto_run_page WHERE run_id=? AND page_id=?', (task['run_id'], task['page_id'])).fetchone()
                if task['run_id'] != run['id'] or task['skip_reason'] != 'fb_auto_page_language_missing' or not untouched(c, dict(task, skip_reason=REASON)) or page['language']:
                    raise RuntimeError('rechecked_task_changed')
                if item['reason'] == 'page_not_eligible':
                    if page['eligible_token_count'] != 0:
                        raise RuntimeError('authorization_reason_not_confirmed')
                    code = 'fb_page_missing_eligible_token'
                else:
                    code = 'fb_auto_no_eligible_video'
                selection = json.loads(task['selection_json'] or '{}')
                selection['language_recheck'] = {'operation_id': audit['operation'], 'original_language': '',
                    'language': 'en', 'original_skip_reason': task['skip_reason'], 'actual_skip_reason': code}
                c.execute('UPDATE fb_auto_task SET skip_reason=?,selection_json=? WHERE id=?', (code, json.dumps(selection), item['id']))
                c.execute("UPDATE fb_auto_run_page SET language='en',skip_reason=? WHERE run_id=? AND page_id=?", (code, task['run_id'], task['page_id']))
                changed.append({'id': item['id'], 'reason': code})
        if args.apply:
            c.commit()
        else:
            c.rollback()
    output = audit_path.with_name('skip-reasons.json' if args.apply else 'skip-reasons-preview.json')
    output.write_text(json.dumps({'applied': args.apply, 'changed': changed}, indent=2)); output.chmod(0o600)
    print(json.dumps({'applied': args.apply, 'changed': len(changed)}))


if __name__ == '__main__':
    main()
