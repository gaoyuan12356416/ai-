#!/usr/bin/env python3
"""Run the tag lane and inherited publishing regressions against one built runtime."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('x_auto_post_validation', 'x_auto_post_store', 'x_auto_post_selector',
           'x_auto_post_service', 'x_auto_post_publisher', 'x_auto_post_runner',
           'x_auto_post_metrics', 'x_post_material_pool', 'x_post_material_pool_selector',
           'x_post_manual_sidecar', 'x_post_manual_runner', 'x_post_schedule_runner')


def run(runtime, output):
    runtime, output = Path(runtime).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    commands = [('tag_revenue', [sys.executable, '-B', str(ROOT / 'scripts/test_x_tag_revenue.py')]),
                ('tag_publish', [sys.executable, '-B', str(ROOT / 'scripts/test_x_tag_publish.py')])]
    commands += [(name, [sys.executable, '-B', '-m', 'unittest', 'scripts.test_' + name]) for name in MODULES]
    results = []
    for name, command in commands:
        started = time.monotonic()
        result = subprocess.run(command, cwd=runtime, text=True, encoding='utf-8',
            capture_output=True, timeout=180,
            env=dict(os.environ, X_TAG_TEST_RUNTIME=str(runtime), PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1'))
        log = result.stdout + result.stderr
        (output / (name + '.log')).write_text(log, encoding='utf-8')
        count = re.search(r'Ran (\d+) tests?', log)
        row = dict(name=name, returncode=result.returncode, count=int(count.group(1)) if count else 0,
                   seconds=round(time.monotonic() - started, 2))
        results.append(row)
        print(json.dumps(row), flush=True)
    summary = dict(passed=all(row['returncode'] == 0 and row['count'] > 0 for row in results),
                   count=sum(row['count'] for row in results), modules=results)
    (output / 'regression-results.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in summary.items() if key != 'modules'}), flush=True)
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.runtime, args.output))
