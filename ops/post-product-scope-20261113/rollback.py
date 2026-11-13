"""Rollback the original release using the reviewed current coordinator API."""
import argparse
import json
from pathlib import Path
import deploy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('sha')
    parser.add_argument('action', choices=('rollback-pause', 'busy', 'rollback'))
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--group', choices=tuple(deploy.GROUPS), required=True)
    parser.add_argument('--reviewed-version', type=int)
    args = parser.parse_args(); release = Path(__file__).resolve().parents[2]
    d = deploy.module(release / 'ops/moboreels-attribution-20261113/deploy.py', 'original_deploy')
    d.verify(release, args.sha)
    directory = args.deployment.resolve()
    d.require(directory.parent == d.DATA / 'deployments' and directory.name.startswith('post-products-'), 'Unexpected deployment')
    state = json.loads((directory / 'manifest.json').read_text())
    d.require(state['commit'] == 'e5b1ee06c07def5262b2eb2888d6cf94f784b8b3', 'Unexpected feature release')
    lanes = deploy.module(release / deploy.OPS / 'lanes.py', 'scoped_lanes')
    if args.action == 'rollback-pause':
        deploy.rollback_pause(d, lanes, directory, state, args.group)
    elif args.action == 'busy':
        deploy.busy(d, directory, state, args.group)
        print(json.dumps({'group': args.group, 'busy': False}))
    else:
        deploy.rollback(d, lanes, directory, state, args.group, args.reviewed_version)


if __name__ == '__main__':
    main()
