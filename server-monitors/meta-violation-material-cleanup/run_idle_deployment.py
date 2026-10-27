#!/usr/bin/env python3
"""One idle cutover attempt; retry only busy, retire this helper on failure."""
import json
import re
import sys
from pathlib import Path

import deploy_when_idle

TIMER = 'meta-violation-material-cleanup-v3-deploy.timer'


def run(argv=None, host=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    code = deploy_when_idle.main(argv)
    if code == 75 or '--apply' not in argv:
        return code
    if code == 0:
        try:
            sha = argv[argv.index('--expected-sha') + 1]
            if not re.fullmatch('[0-9a-f]{40}', sha):
                raise ValueError('invalid_sha')
            receipt = json.loads((deploy_when_idle.BASE / 'deployments' / (sha + '.json')).read_text(encoding='utf-8'))
            if (receipt.get('status') != 'applied' or receipt.get('commit') != sha
                    or receipt.get('timer_restore_pending')
                    or (deploy_when_idle.BASE / 'current').resolve() != Path(receipt['candidate_release'])):
                raise ValueError('completion_unconfirmed')
        except Exception:
            print(json.dumps({'status': 'error', 'code': 'deployment_completion_unconfirmed'}))
            code = 1
    try:
        # A failed cutover needs manual reconciliation, even if a full disk
        # prevented the deployer from recording its independent failure fence.
        (host or deploy_when_idle.Host()).command(['systemctl', 'disable', '--now', TIMER])
    except Exception:
        print(json.dumps({'status': 'error', 'code': 'deployment_timer_retirement_pending'}))
        return 1
    return code


if __name__ == '__main__':
    raise SystemExit(run())
