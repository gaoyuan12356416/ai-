"""Reclassify the latest collected facts after an alias fix; never recollect income."""
import argparse
from contextlib import closing
import fcntl
import json
from pathlib import Path
import sqlite3
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime', default='/opt/meta-page-recovery/current')
    p.add_argument('--root', default='/mnt/data-disk/fb-page-portraits')
    p.add_argument('--env', default='/root/drama_material_service/.env')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    sys.path.insert(0, str(Path(args.runtime).resolve()))
    from features.fb_page_portrait.build import build
    from features.fb_page_portrait.collect import environment, Reader
    from features.fb_page_portrait.runtime import atomic_json
    from features.fb_page_portrait.storage import write, publish
    from features.x_tag_revenue.tags import VERSION
    root = Path(args.root)
    with (root / 'refresh.lock').open('a') as lock:
        # An active collector owns this lock. Let it finish and retry later.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = json.loads((root / 'current.json').read_text())
        raw = json.loads((root / (previous['version'] + '.input.json')).read_text())
        data = build(raw)
        fields = ('paid_cents', 'social_cents', 'spend_cents', 'revenue_cents')
        with closing(sqlite3.connect((root / previous['database']).as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
            old = {pid: json.loads(value) for pid, value in conn.execute('SELECT page_id,summary FROM profiles')}
        assert set(data['pages']) == set(old), 'page_scope_changed'
        for pid, profile in data['pages'].items():
            assert all(profile[k] == old[pid][k] for k in fields), 'income_facts_changed:' + pid
        data['meta'].update(tag_mapping_version=VERSION, reclassified_from=previous['version'])
        atomic_json(root / (data['meta']['version'] + '.input.json'), raw)
        env = environment(args.env)
        proof = write(data, env, Reader(env))
        atomic_json(root / (data['meta']['version'] + '.receipt.json'), proof)
        publish(data, proof, root)
        atomic_json(root / 'status.json', dict(status='ok', **data['meta']))
        result = dict(previous=previous, current=data['meta'], verified_rows=proof['verified_rows'],
                      income_conserved=True)
        atomic_json(args.output, result)
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
