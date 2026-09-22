"""Migrate dated analysis scripts to leased snapshots, preserving exported reports.

Only exact single-line Path literals under the known snapshot directory change.
Old scripts are backed up outside the reference roots. No scripts are executed.
Historical reruns use current cached data, not the retired point-in-time inputs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

PATTERN = re.compile(r'''Path\((["'])/mnt/data-disk/tt-minis-storage/analysis-snapshots/(tt_minis_cache_[A-Za-z0-9_]+\.sqlite3)\1\)''')
NAME = re.compile(r'tt_minis_cache_[A-Za-z0-9_]+\.sqlite3')


def rewrite(text, owner):
    def replace(match):
        return 'Path(__import__("tt_minis_storage").snapshot(owner=' + repr(owner) + '))'
    rewritten, count = PATTERN.subn(replace, text)
    if count and NAME.search(rewritten):
        raise RuntimeError('Unrecognized remaining snapshot reference')
    if count:
        compile(rewritten, owner, 'exec')
    return rewritten, count


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, default=Path('/root/codex_test'))
    p.add_argument('--audit', type=Path, required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    changes = []
    for path in args.root.glob('*.py'):
        old = path.read_text(encoding='utf-8')
        new, count = rewrite(old, 'legacy-analysis:' + path.stem)
        if count:
            changes.append((path, old, new))
    if args.apply:
        args.audit.mkdir(parents=True, exist_ok=False)
    receipt = []
    for path, old, new in changes:
        before = hashlib.sha256(old.encode()).hexdigest()
        if args.apply:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == before
            (args.audit / (path.name + '.bak')).write_bytes(old.encode())
            target = path.resolve()
            tmp = target.with_name(target.name + '.snapshot-migration.tmp')
            tmp.write_bytes(new.encode())
            os.chmod(str(tmp), target.stat().st_mode)
            os.replace(str(tmp), str(target))
        receipt.append({'path': str(path), 'before_sha256': before,
                        'after_sha256': hashlib.sha256(new.encode()).hexdigest()})
    if args.apply:
        (args.audit / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps({'apply': args.apply, 'changed_count': len(receipt), 'changes': receipt}))


if __name__ == '__main__':
    main()
