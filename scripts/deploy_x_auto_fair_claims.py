"""Reuse the single-file X Auto deployment with scoped drain and rollback."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import deploy_x_pending_launch as ops

ops.FILE = 'features/x_auto_posts/core.py'
ops.BEFORE = '4f66553835639d46a89b0ce689f72e1ee1058fda90abaade2d3d57084b5c6d54'
ops.BACKUP_NAME = 'core'
ops.TESTS = [('test_x_auto_fair_claims.py', []),
             ('test_drama_launch_range.py', ['--platform', 'x']),
             ('test_x_pending_launch.py', [])]

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback', 'verify'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.action == 'prepare': ops.prepare(folder)
    elif args.action == 'verify': ops.verify(folder)
    else: ops.switch(folder, args.action == 'rollback')
