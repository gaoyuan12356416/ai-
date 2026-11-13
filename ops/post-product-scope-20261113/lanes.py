"""Reviewed operation fragment. Defines functions only; importing/running does not drain anything."""
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

EXPECTED_ROOT = '/mnt/data-disk/tt-metric-wait-20261113/deployment-20261113T024331Z/coordinator'
DB = '/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3'


def _save_receipt(path, body):
    path = Path(path)
    assert path.parent.is_dir() and str(path.resolve()).startswith('/mnt/data-disk/moboreels-library/deployments/')
    temp = path.with_name(path.name + '.next')
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(body, handle, ensure_ascii=False, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    os.replace(str(temp), str(path))
    directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def set_lane_scoped(lane, enabled, expected_version, expected_enabled, receipt_path,
                    *, preparation_only=False):
    """Call the current Store method with an atomic version/state guard, without __init__ migrations."""
    assert lane in ('x', 'fb', 'tt', 'youtube')
    assert isinstance(enabled, bool) and isinstance(expected_enabled, bool) and isinstance(preparation_only, bool)
    root = Path('/opt/pipeline-dispatch/current').resolve()
    assert str(root) == EXPECTED_ROOT, 'Coordinator current changed; re-review source'
    assert hashlib.sha256((root / 'features/pipeline_dispatch/core.py').read_bytes()).hexdigest() == 'ec9e87d82fe4bda42748a6f953a01169c5b0b778742fa62bad8207b943f2f113', 'Coordinator code changed'
    assert not Path(receipt_path).exists(), 'Keep the original operation receipt; reconcile it first'
    sys.path.insert(0, str(root))
    from features.pipeline_dispatch.core import DispatchStore, utcnow
    import features.pipeline_dispatch.core as module
    assert Path(module.__file__).resolve() == root/'features/pipeline_dispatch/core.py'
    # A normal constructor migrates schema and refreshes isolation resources. This fragment deliberately
    # binds only the existing Store's pure config methods needed by channel_enabled.
    store = object.__new__(DispatchStore)
    store.path, store.clock, store.capability_exclusions = DB, utcnow, {}
    receipt = {'lane': lane, 'target_enabled': enabled, 'expected_version': expected_version,
               'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'coordinator_root': str(root), 'status': 'intent', 'changed': False}

    @contextlib.contextmanager
    def guarded_transaction():
        conn = store.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            before = store.config(conn)
            assert before['version'] == expected_version, 'Coordinator version changed; no write performed'
            assert (lane in before['enabled_channels']) == expected_enabled, 'Lane changed; no write performed'
            receipt['before'] = {'version': before['version'], 'enabled': expected_enabled,
                'preparation_only': lane in before.get('preparation_only_channels', []),
                'enabled_channels': before['enabled_channels'],
                'preparation_only_channels': before.get('preparation_only_channels', [])}
            assert expected_enabled != enabled, 'No-op is not a deployment-owned state change'
            _save_receipt(receipt_path, receipt)
            yield conn
            after = store.config(conn)
            assert after['version'] == expected_version + 1
            expected = dict(before, version=expected_version+1,
                enabled_channels=sorted((set(before['enabled_channels']) | {lane}) if enabled else (set(before['enabled_channels']) - {lane})),
                preparation_only_channels=sorted((set(before.get('preparation_only_channels', [])) | {lane}) if enabled and preparation_only else (set(before.get('preparation_only_channels', [])) - {lane})))
            assert after == expected, 'Unexpected unrelated config change; rollback transaction'
            conn.commit()
            receipt.update(status='committed', changed=True, after_version=after['version'])
            _save_receipt(receipt_path, receipt)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    store.transaction = guarded_transaction
    store.channel_enabled(lane, enabled, preparation_only=preparation_only)
    return {'lane': lane, 'enabled': enabled, 'version': receipt['after_version'], 'receipt': str(receipt_path)}


def reopen_original_lane(pause_receipt, resume_receipt, *, reviewed_current_version=None):
    """Restore only a lane this deployment actually changed from enabled to disabled."""
    saved = json.loads(Path(pause_receipt).read_text())
    assert saved['status'] == 'committed' and saved['changed']
    assert saved['before']['enabled'] and saved['target_enabled'] is False
    version = saved['after_version'] if reviewed_current_version is None else reviewed_current_version
    # Override only after reviewing intervening config events. Other-lane settings are never restored wholesale.
    return set_lane_scoped(saved['lane'], True, version, False, resume_receipt,
                           preparation_only=saved['before']['preparation_only'])

# Example, to be added explicitly by the operator after a fresh read-only version check:
# result = set_lane_scoped('x', False, EXPECTED_CONFIG_VERSION, True, DEPLOYMENT+'/private/x-drain.json')
# Wait for x coordinator active leases and exact owner busy checks to reach zero; deploy the x phase.
# result = reopen_original_lane(DEPLOYMENT+'/private/x-drain.json', DEPLOYMENT+'/private/x-reopen.json')
# For TT, keep one tt drain active across both tt and tt_auto phases, then reopen once.
