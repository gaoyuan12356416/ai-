"""Deploy bounded Creative reference reads after all deletion/recheck runs settle."""
import argparse
from pathlib import Path

import deploy_meta_asset_recovery as rollout
from deploy_meta_delete_throughput import require_api_stopped
from deploy_meta_preview_scale import no_writes


BASE = '16eb6d9e8277983264da8cac1e87b5ff3d1b990e'
FILES = ['features/fb_ad_asset_delete/'+name+'.py' for name in ('source', 'service', 'store')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    rollout.BASE, rollout.BASELINE_HASHES, rollout.FILES, rollout.idle = BASE, {}, FILES, no_writes
    if args.mode == 'prepare':
        rollout.prepare(args.path)
    else:
        require_api_stopped()
        rollout.switch(args.path, args.mode == 'rollback')


if __name__ == '__main__':
    main()
