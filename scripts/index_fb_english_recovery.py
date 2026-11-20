"""Offer only audited recovered FB rows through the normal dispatch projection."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    audit = json.loads(Path(args.audit).read_text())
    assert audit['applied']
    ids = [t['id'] for r in audit['runs'] for t in r['restored']]
    root = Path('/opt/pipeline-dispatch/current').resolve()
    sys.path.insert(0, str(root))
    from features.pipeline_dispatch.service import build_registry
    from features.pipeline_dispatch.core import DispatchStore, utcnow
    from features.pipeline_dispatch.owners import OwnerResolver
    pid = subprocess.check_output(['systemctl', 'show', 'pipeline-coordinator.service', '-p', 'MainPID', '--value'], text=True).strip()
    os.environ.update(dict(x.split('=', 1) for x in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in x))
    config = json.loads(Path('/etc/pipeline-coordinator.json').read_text())
    adapter = build_registry(config).adapters[('fb', 'auto')]
    owner = OwnerResolver(config.get('paths', {}).get('youtube'))
    store = DispatchStore.__new__(DispatchStore)
    store.path, store.clock = config['db_path'], utcnow
    store.capability_exclusions = {k: set(v) for k, v in config.get('node_capability_exclusions', {}).items()}
    result = {'applied': args.apply, 'runtime': str(root), 'offered': [], 'already_indexed': [], 'retained': []}
    for tid in ids:
        key = 'fb:auto:' + str(tid)
        with contextlib.closing(store.connect()) as c:
            if c.execute('SELECT 1 FROM pipeline_job WHERE source_key=?', (key,)).fetchone():
                result['already_indexed'].append(tid); continue
        row = adapter.read(tid)
        recovery = json.loads(row.get('selection_json') or '{}').get('recovery', {}) if row else {}
        if not row or row['status'] != 'planned' or row.get('unknown_outcome') or row.get('graph_post_id') or row.get('attempt_count') or recovery.get('operation_id') != audit['operation']:
            result['retained'].append(tid); continue
        offer = adapter.project(row)
        assert offer['source_key'] == key and offer['stage'] == 'prepare'
        assert offer['execution_available'] and not offer['reconcile_only'] and not offer['legacy_inflight']
        offer = owner.resolve([offer])[0]
        if args.apply:
            stored = store.offer(offer)
            assert stored and stored['source_key'] == key
        result['offered'].append(tid)
    output = Path(args.audit).with_name('indexed.json' if args.apply else 'index-preview.json')
    output.write_text(json.dumps(result, indent=2)); output.chmod(0o600)
    print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in result.items()}))


if __name__ == '__main__':
    main()
