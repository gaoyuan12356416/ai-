"""Recover only future, untouched blank-language slots using the active planner's rules."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

REASON = 'fb_auto_page_language_missing'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--folder', required=True)
    p.add_argument('--run-ids', type=int, nargs='+', required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    runtime = Path('/opt/fb-auto-post/current').resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(runtime))
    from scripts.recover_fb_skipped_runs import existing_store, service_env, repositories, save
    from features.fb_auto_posts import core
    from features.fb_auto_posts.skipped_recovery import untouched, published_materials, REASON as OLD_REASON
    from features.fb_auto_posts.strategy import daily_limit
    from features.story_copy.bridge import fb_plan

    folder = Path(args.folder)
    if not folder.resolve().is_relative_to('/mnt/data-disk'):
        raise ValueError('audit_requires_data_disk')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.apply:
        preview = json.loads((folder / 'preview.json').read_text())
        assert not preview['applied'] and [r['id'] for r in preview['runs']] == args.run_ids
        assert preview['runtime'] == str(runtime)
    operation = hashlib.sha256(str(folder).encode()).hexdigest()[:32]
    env = service_env()
    # Configuration stays in memory; catalog reads and caption helpers use the
    # same configured services as the active owner. No platform write is called.
    os.environ.update(env)
    store = existing_store(env['FB_AUTO_POST_DB_PATH'])
    snapshot_path = folder / ('before-apply.sqlite3' if args.apply else 'preview.sqlite3')
    if snapshot_path.exists():
        raise RuntimeError('audit_destination_exists')
    with contextlib.closing(sqlite3.connect(Path(store.path).as_uri() + '?mode=ro', uri=True, timeout=30)) as src:
        with contextlib.closing(sqlite3.connect(snapshot_path)) as dest:
            src.backup(dest)
            assert dest.execute('pragma quick_check').fetchone()[0] == 'ok'
    snapshot_path.chmod(0o600)
    if not args.apply:
        store = existing_store(snapshot_path)
    page_repo, materials = repositories(env, operation)
    report = {'runtime': str(runtime), 'applied': args.apply, 'operation': operation, 'runs': []}
    for rid in args.run_ids:
        with store.connect() as c:
            run = dict(c.execute('SELECT * FROM fb_auto_run WHERE id=?', (rid,)).fetchone())
            template = dict(c.execute('SELECT t.*,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.id=?', (run['template_id'],)).fetchone())
            assert run['trigger_type'] == 'auto' and template['status'] == 'enabled'
            assert run['template_version'] == template['current_version'] and run['config_json'] == template['config_json']
            targets = {r[0] for r in c.execute("SELECT page_id FROM fb_auto_task WHERE run_id=? AND status='skipped' AND skip_reason=? AND planned_publish_at_utc>?", (rid, REASON, core.utc_iso(store.now_fn())))}
        config = json.loads(run['config_json'])
        assert str(config.get('app_id')) == '1479' and not core.moboreels_fb.contains(config['message_template'])
        def current_pages():
            return [p for p in page_repo.list_pages(config['group_ids'], is_admin=bool(template['scope_is_admin']), owner_user_id=template['owner_user_id']) if p.page_id in targets]
        pages = current_pages()
        eligible = [p for p in pages if p.eligible_token_count > 0 and core.page_language(p.language) == 'en' and daily_limit(config, p.page_id) > 0]
        with store.connect() as c:
            caption = core.page_caption.load(c, template['created_by'], [p.page_id for p in eligible])
        profiles = core.load_profiles([p.page_id for p in eligible])
        identity = [run['template_id'], run['template_version'], run['slot_key']]
        profile_enabled = any(not core.Decision(p.page_id, identity, profiles).reason for p in eligible)
        snapshots = core.page_caption.candidate_snapshots(dict(config, _candidate_limit=5000,
            _profile_selection=profile_enabled, _page_caption_description=core.page_caption.needs_description(caption)), eligible, materials, caption)
        pages = {p.page_id: p for p in current_pages()}
        if page_repo.legacy_conflicts(config['group_ids']):
            raise RuntimeError('legacy_queue_conflict')
        restored, retained = [], []
        with store._lock, store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = c.execute('SELECT status,current_version FROM fb_auto_template WHERE id=?', (run['template_id'],)).fetchone()
            assert tuple(current) == ('enabled', run['template_version'])
            now_dt = store.now_fn(); now = core.utc_iso(now_dt)
            scores = core.load_scores(c, config, now_dt)
            tasks = c.execute('SELECT * FROM fb_auto_task WHERE run_id=? AND skip_reason=? ORDER BY planned_publish_at_utc,id', (rid, REASON)).fetchall()
            for raw in tasks:
                task = dict(raw); tid = task['id']; page = pages.get(task['page_id'])
                planned = core.datetime.fromisoformat(task['planned_publish_at_utc'])
                if planned <= now_dt or planned.astimezone(core.BEIJING).date() != now_dt.astimezone(core.BEIJING).date():
                    continue
                reason = ''
                if not untouched(c, dict(task, skip_reason=OLD_REASON)):
                    reason = 'existing_execution_evidence'
                frozen = c.execute('SELECT * FROM fb_auto_run_page WHERE run_id=? AND page_id=?', (rid, task['page_id'])).fetchone()
                if not page or not frozen or frozen['language'] or page.owner_user_id != frozen['owner_user_id'] or task['group_id'] not in page.group_ids:
                    reason = reason or 'page_scope_changed'
                elif page.eligible_token_count <= 0 or core.page_language(page.language) != 'en' or daily_limit(config, page.page_id) == 0:
                    reason = reason or 'page_not_eligible'
                if not reason:
                    reason = core.automatic_block(c, page.page_id) or core.manual_block(c, page.page_id)
                if reason:
                    retained.append({'id': tid, 'reason': reason}); continue
                snap = snapshots.get(page.page_id)
                decision = core.Decision(page.page_id, identity, profiles)
                pool = snap.candidates if snap else ()
                if snap and (decision.reason or snap.profile_error):
                    pool = core.diverse_candidates(pool, 5000, 20 if config.get('drama_cooldown_hours') else 0)
                blocked = core.cooldown_content_ids(c, page.page_id, task['planned_publish_at_utc'], int(config.get('drama_cooldown_hours', 0)), now)
                candidates = [m for m in pool if core.page_language(m.language) == 'en' and m.content_id not in blocked
                              and m.deploy_time <= planned.timestamp() and core.page_caption.material_eligible(config, page.page_id, m, caption)]
                excluded = store._cooldown_material_ids(c, page.page_id, int(config['cooldown_days'])) | published_materials(c, page.page_id)
                material, selection = core.choose_profile_material(candidates, excluded, decision=decision,
                    snapshot=snap, candidate_limit=5000, materials=materials, config=config,
                    page_id=page.page_id, slot_key=run['slot_key'], scores=scores) if snap else (None, {})
                if material is None:
                    retained.append({'id': tid, 'reason': 'no_current_eligible_unpublished_english_material'}); continue
                reason = core.automatic_block(c, page.page_id, material.material_id) or core.manual_block(c, page.page_id, material.material_id)
                if reason:
                    retained.append({'id': tid, 'reason': reason}); continue
                # Keep task ID, frozen target binding and original future time.
                selection['recovery'] = {'operation_id': operation, 'original_skip_reason': REASON,
                    'rechecked_at_utc': now, 'original_language': '', 'language': 'en'}
                c.execute("UPDATE fb_auto_task SET status='planned',skip_reason='',error_code='',error_message='',material_id=?,content_id=?,source_media_url=?,message_text=?,completed_at_utc='' WHERE id=?",
                          (material.material_id, material.content_id, material.media_url, store._message(config, material), tid))
                c.execute("UPDATE fb_auto_run_page SET language='en',snapshot_status='eligible',skip_reason='',eligible_token_count=? WHERE run_id=? AND page_id=?", (page.eligible_token_count, rid, page.page_id))
                selection['caption'] = core.page_caption.freeze(store, c, config, page, material, tid, int(now_dt.timestamp()), caption)
                fb_plan(selection, store, config, page, material, tid, int(now_dt.timestamp()))
                c.execute('UPDATE fb_auto_task SET selection_json=? WHERE id=?', (json.dumps(selection, sort_keys=True), tid))
                restored.append({'id': tid, 'page_id': page.page_id, 'language': 'en', 'material_id': material.material_id, 'planned_at': task['planned_publish_at_utc']})
            if restored:
                c.execute('UPDATE fb_auto_run SET queued_tasks=queued_tasks+?,skipped_tasks=skipped_tasks-? WHERE id=?', (len(restored), len(restored), rid))
                store._refresh_run(c, rid, now)
            c.commit()
        report['runs'].append({'id': rid, 'restored': restored, 'retained': retained})
        save(folder / ('applied.json' if args.apply else 'preview.json'), report)
        print(json.dumps({'run': rid, 'applied': args.apply, 'restored': len(restored), 'retained': len(retained)}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': getattr(exc, 'code', type(exc).__name__)}), flush=True)
        import traceback
        print(json.dumps({'frames': [(Path(f.filename).name, f.lineno, f.name) for f in traceback.extract_tb(exc.__traceback__)]}))
        sys.exit(1)
