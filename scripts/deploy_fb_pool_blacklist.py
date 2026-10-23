"""Scoped Page-pool blacklist waiver; no database migration or historical retry."""
import importlib.util
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    '_pool_blacklist_release', Path(__file__).with_name('deploy_fb_page_list_performance.py'))
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)
release.BASELINE = 'cd9e068'
release.REMOTE_REF = 'refs/heads/codex/fb-pool-blacklist-20261023'
release.BACKEND_FILES = {'features/fb_video_publish/' + n + '.py' for n in ('pool', 'pool_scheduler', 'worker')}
release.STATIC_FILES = set()
release.ALLOWED = release.BACKEND_FILES
base, pool = release.base, release.pool_release
_rollback = release.rollback
_stage_targets = release.stage_targets
# Effects reads Meta/MySQL and writes only fb_auto_post_effect* directly. It
# neither imports the changed modules nor calls the sidecar being restarted.
# Keep it running; all publishing/preparing and new-worker leases still drain.
base.OLD_NAMES = tuple(name for name in base.OLD_NAMES if name != 'effects')


def verify_independent_effects():
    for relative in ('scripts/fb_auto_post_effect_runner.py', 'scripts/fb_auto_post_metric_runner.py',
                     'features/fb_auto_posts/effect_collector.py', 'features/fb_auto_posts/effects.py'):
        base.require(base.sha(base.FB / relative) == base.sha(release.ROOT / relative),
                     'Independent effects source drift; review before skipping its drain: ' + relative)


def stage_targets():
    verify_independent_effects()
    return _stage_targets()


def require_no_pending_pool():
    with base.database() as conn:
        items = conn.execute("SELECT COUNT(*) FROM fb_video_pool_item WHERE status IN ('pending','processing')").fetchone()[0]
        tasks = conn.execute("""SELECT COUNT(*) FROM fb_video_task
            WHERE (source_kind='pool' OR json_type(snapshot_json,'$.pool') IS NOT NULL)
              AND (status NOT IN ('published','failed','cancelled') OR unknown_outcome<>0)""").fetchone()[0]
    base.require(not items and not tasks,
                 'Reconcile pending pool items/tasks before restoring the blacklist; keep current database and links')


def rollback(manifest_path):
    verify_independent_effects()
    require_no_pending_pool()
    original = pool.quiesce
    def guarded(saved):
        original(saved)
        require_no_pending_pool()
    pool.quiesce = guarded
    try:
        return _rollback(manifest_path)
    finally:
        pool.quiesce = original


def apply_release(targets, commit):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = base.BASE / 'backups' / ('pool-blacklist-' + stamp + '-' + commit[:8])
    backup.mkdir(parents=True)
    saved = pool.saved_states()
    base.require(saved[base.FB_UNIT]['active'] == 'active', 'Sidecar must be healthy before patch')
    manifest_path = backup / 'manifest.json'
    manifest = dict(commit=commit, baseline=release.BASELINE, states=saved, files=[], phase='prepared',
                    main_api_restarted=False)
    for i, (source, target, mode) in enumerate(targets):
        destination = backup / ('file-' + str(i))
        shutil.copy2(target, destination)
        manifest['files'].append(dict(target=str(target), backup=str(destination), before=base.sha(target),
                                      after=base.sha(source), mode_before=mode))
    pool.json_save(manifest_path, manifest)
    modified = exposed = False
    try:
        release.assert_pool_window()
        pool.quiesce(saved)
        release.assert_pool_window()
        base.snapshot_db(backup / 'operational.sqlite3')
        for (source, target, _), item in zip(targets, manifest['files']):
            base.require(base.sha(target) == item['before'], 'Target changed during drain')
            base.require(base.sha(Path(item['backup'])) == item['before'], 'Backup changed during drain')
            base.require(base.sha(source) == item['after'], 'Release changed during drain')
        modified = True
        for source, target, mode in targets:
            base.atomic_bytes(source.read_bytes(), target, mode)
        for item in manifest['files']:
            base.require(base.sha(Path(item['target'])) == item['after'], 'Installed hash mismatch')
        manifest['phase'] = 'files_installed'
        pool.json_save(manifest_path, manifest)
        # Once the new sidecar may accept items, never automatically restore a
        # stricter policy over those accepted items, even if health/resume fails.
        exposed = True
        release.resume(saved)
        manifest['phase'] = 'deployed'
        pool.json_save(manifest_path, manifest)
        print(json.dumps(dict(deployed=True, commit=commit, manifest=str(manifest_path), files=len(targets),
                              database_migrated=False, main_api_restarted=False, service_states_preserved=True)), flush=True)
    except Exception:
        if exposed:
            manifest['phase'] = 'needs_service_reconciliation_new_code_retained'
            pool.json_save(manifest_path, manifest)
            raise
        if modified:
            release.restore_files(manifest)
        release.resume(saved)
        manifest['phase'] = 'failed_restored'
        pool.json_save(manifest_path, manifest)
        raise


release.apply_release = apply_release
release.rollback = rollback
release.stage_targets = stage_targets
if __name__ == '__main__':
    release.main()
