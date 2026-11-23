"""Switch only the health-tool release; never starts or restarts a service."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path

POINTER = Path('/opt/publish-scheduler-health/current')
EXPECTED = '34e3d91fa2109175b27afa72fcd583aa6ce297eaffc40994f02c90f7b1521b6e'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def switch(target):
    temporary = POINTER.with_name('current.next-' + str(os.getpid()))
    temporary.symlink_to(target)
    temporary.replace(POINTER)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['apply', 'rollback'])
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    state = Path(args.state)
    manifest_path = state / 'manifest.json'
    source = Path(__file__).resolve().parents[1]
    relative = 'scripts/publish_scheduler_health.py'
    if args.action == 'apply':
        if not POINTER.is_symlink() or digest(POINTER / relative) != EXPECTED:
            raise RuntimeError('health_tool_changed_since_review')
        state.mkdir(parents=True, exist_ok=False, mode=0o700)
        previous = POINTER.resolve()
        manifest = {'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'before': str(previous), 'after': str(source),
                    'before_sha256': EXPECTED, 'after_sha256': digest(source / relative)}
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        if POINTER.resolve() != previous:
            raise RuntimeError('health_tool_pointer_changed')
        switch(source)
    else:
        manifest = json.loads(manifest_path.read_text())
        if POINTER.resolve() != Path(manifest['after']) or digest(POINTER / relative) != manifest['after_sha256']:
            raise RuntimeError('health_tool_changed_since_deployment')
        if digest(Path(manifest['before']) / relative) != manifest['before_sha256']:
            raise RuntimeError('previous_health_tool_changed')
        switch(manifest['before'])
    print(json.dumps({'action': args.action, 'current': str(POINTER.resolve()),
                      'state': str(state), 'services_restarted': False}))


if __name__ == '__main__':
    main()
