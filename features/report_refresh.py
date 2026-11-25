"""Start the existing report collector and follow its atomic publication point."""
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs


REPORTS = {
    'socialkit': ('socialkit-analytics-refresh.service', Path('/mnt/data-disk/socialkit-analytics')),
    'social': ('social-analytics-warehouse-refresh.service', Path('/mnt/data-disk/social-analytics-warehouse')),
}
ACTIVE = {'activating', 'active', 'reloading'}


def systemctl(*args):
    result = subprocess.run(['/usr/bin/systemctl', *args], check=True, timeout=8,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'})
    return result.stdout


def unit_state(unit):
    lines = systemctl('show', '-p', 'ActiveState', '-p', 'ExecMainStartTimestamp', unit)
    return dict(line.split('=', 1) for line in lines.splitlines())


def current_snapshot(root):
    try:
        return json.loads((root / 'current.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def start(unit, root):
    state = unit_state(unit)
    current = current_snapshot(root)
    after = current.get('version', '')
    if state['ActiveState'] in ACTIVE:
        # A collector may still be cleaning up after publishing its new manifest.
        started = state.get('ExecMainStartTimestamp')
        if started and started != 'n/a' and after:
            stamp = datetime.strptime(started, '%a %Y-%m-%d %H:%M:%S UTC')
            if after[:15] >= stamp.strftime('%Y%m%dT%H%M%S'):
                return dict(state='complete', version=after)
    else:
        # Concurrent manual and timer starts are coalesced by the same oneshot unit.
        systemctl('start', '--no-block', unit)
    return dict(state='running', after=after)


def progress(unit, root, after):
    state = unit_state(unit)
    version = current_snapshot(root).get('version')
    if version and version != after:
        return dict(state='complete', version=version)
    if state['ActiveState'] in ACTIVE:
        return dict(state='running', after=after)
    return dict(state='failed', message='本轮抓取未生成新版报表，可重新抓取。')


def dispatch(handler, parsed, report, reply):
    unit, root = REPORTS[report]
    try:
        if handler.command == 'POST':
            handler.close_connection = True
            if handler.headers.get('X-Report-Refresh') != '1':
                return reply(403, dict(message='请从报表页面发起抓取。'))
            if parsed.query or handler.headers.get('Content-Length', '0') != '0' or handler.headers.get('Transfer-Encoding'):
                return reply(400, dict(message='抓取请求不接受参数。'))
            result = start(unit, root)
            return reply(202 if result['state'] == 'running' else 200, result)
        if handler.command == 'GET':
            params = parse_qs(parsed.query, keep_blank_values=True)
            after = params.get('after', [])
            if set(params) != {'after'} or len(after) != 1 or not re.fullmatch(r'[A-Za-z0-9-]{0,64}', after[0]):
                return reply(400, dict(message='抓取状态参数无效。'))
            return reply(200, progress(unit, root, after[0]))
        return reply(405, dict(message='该请求方式不受支持。'))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        logging.getLogger(__name__).exception('report refresh control failed for %s', report)
        return reply(503, dict(message='暂时无法确认抓取状态，请稍后重试。'))
