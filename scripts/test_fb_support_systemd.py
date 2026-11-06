"""Isolated systemd rehearsal. Only temporary codex-fb-support-proof-* units."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

if __package__:
    from . import publish_scheduler_health as health
else:
    import publish_scheduler_health as health


def run(*args, check=True):
    return subprocess.run(['systemctl', *args], capture_output=True, text=True, timeout=15, check=check)


def main():
    assert os.geteuid() == 0
    prefix = 'codex-fb-support-proof-' + str(os.getpid())
    main_unit, helper, paused, failure, timer, oneshot = [prefix + s for s in
        ('-main.service','-worker.service','-paused.service','-failure.service','-job.timer','-job.service')]
    units = {
        main_unit: '[Unit]\nWants=' + helper + ' ' + failure + '\n[Service]\nExecStart=/bin/sleep infinity\n',
        helper: '[Unit]\nRequires=' + main_unit + '\nAfter=' + main_unit + '\n[Service]\nExecStart=/bin/sleep infinity\n',
        paused: '[Service]\nExecStart=/bin/sleep infinity\n',
        failure: '[Service]\nExecStart=/bin/false\n',
        timer: '[Timer]\nOnActiveSec=1h\nUnit=' + oneshot + '\n',
        oneshot: '[Service]\nType=oneshot\nExecStart=/bin/true\n',
    }
    root = Path('/run/systemd/system')
    paths = [root / unit for unit in units]
    assert all(not path.exists() and path.parent == root and path.name.startswith(prefix) for path in paths)
    health.LANES['proof'] = {'daemons': [main_unit, helper, paused], 'timers': {timer: 3600}}
    evidence = {}
    try:
        for path in paths:
            path.write_text(units[path.name])
        run('daemon-reload')
        run('start', main_unit, timer)
        assert run('is-active', main_unit).stdout.strip() == 'active'
        assert run('show', failure, '-p', 'ActiveState', '--value').stdout.strip() in ('failed', 'inactive')
        evidence['failed_wanted_helper_does_not_block_parent'] = True
        with tempfile.TemporaryDirectory(prefix=prefix) as folder:
            for fail in (False, True):
                try:
                    with health.deployment_guard(['proof'], Path(folder) / str(fail)):
                        run('stop', main_unit, timer)
                        assert run('show', helper, '-p', 'ActiveState', '--value').stdout.strip() == 'inactive'
                        if fail:
                            raise ValueError('rehearsed_deployment_failure')
                except ValueError as exc:
                    assert str(exc) == 'rehearsed_deployment_failure'
                for unit in (main_unit, helper, timer):
                    assert run('is-active', unit).stdout.strip() == 'active'
                assert run('show', paused, '-p', 'ActiveState', '--value').stdout.strip() == 'inactive'
                assert run('show', oneshot, '-p', 'ExecMainStartTimestampMonotonic', '--value').stdout.strip() == '0'
                proof = json.loads((Path(folder) / str(fail) / 'support-restored.json').read_text())
                assert proof['ok'] and proof['deployment_failed'] == fail
                evidence['failed_exit_restored' if fail else 'successful_exit_restored'] = True
            # Even a wanted helper that was deliberately paused stays paused.
            run('stop', helper)
            with health.deployment_guard(['proof'], Path(folder) / 'paused'):
                run('stop', main_unit)
            assert run('show', helper, '-p', 'ActiveState', '--value').stdout.strip() == 'inactive'
            evidence['paused_preserved_and_oneshot_not_replayed'] = True
    finally:
        run('stop', *units, check=False)
        run('reset-failed', *units, check=False)
        for path in paths:
            if path.exists():
                path.unlink()
        run('daemon-reload')
        del health.LANES['proof']
    print(json.dumps(evidence))


if __name__ == '__main__':
    main()
