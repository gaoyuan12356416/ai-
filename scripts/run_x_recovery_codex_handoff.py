"""Run the explicitly requested one-off CPU handoff and deliver its final reply.

Production changes remain the child's GitHub-first responsibility. Credentials
are read only in memory from the existing Feishu plugin configuration.
"""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import uuid


def write_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def request_json(url, data, token=None):
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.load(response)
    if result.get('code', -1) != 0:
        raise RuntimeError('Feishu rejected request, code=' + str(result.get('code')))
    return result


def notify(config_path, recipient, message, message_uuid):
    config = json.loads(config_path.read_text())
    auth = request_json('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                        {'app_id': config['appId'], 'app_secret': config['appSecret']})
    result = request_json('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
                          {'receive_id': recipient, 'msg_type': 'text', 'uuid': message_uuid,
                           'content': json.dumps({'text': message}, ensure_ascii=False)},
                          auth['tenant_access_token'])
    return result.get('data', {}).get('message_id', '')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--recipient', required=True)
    parser.add_argument('--feishu-config', type=Path,
                        default=Path('/root/.codex/plugins/feishu/config.json'))
    args = parser.parse_args()
    os.umask(0o077)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = directory / 'run.json'
        if state_path.exists():
            raise RuntimeError('This one-off handoff already started; inspect it before resuming')
        state = {'state': 'starting', 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'runner_pid': os.getpid(), 'message_uuid': str(uuid.uuid4())}
        write_json(state_path, state)
        final = directory / 'final.txt'
        code = -1
        try:
            with (directory / 'prompt.txt').open('r') as prompt, (directory / 'events.jsonl').open('w') as events, (directory / 'stderr.log').open('w') as errors:
                child = subprocess.Popen(['/usr/bin/codex', 'exec', '--cd', str(args.repository),
                                          '--sandbox', 'danger-full-access', '-c', 'approval_policy="never"',
                                          '--json', '--color', 'never', '--output-last-message', str(final), '-'],
                                         stdin=prompt, stdout=events, stderr=errors)
                state.update(state='codex_running', codex_pid=child.pid)
                write_json(state_path, state)
                code = child.wait()
        except Exception as exc:
            state['runner_error_type'] = type(exc).__name__
        state.update(state='codex_exited', exit_code=code,
                     finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        write_json(state_path, state)
        body = final.read_text(encoding='utf-8').strip() if final.exists() else ''
        if not body:
            body = '远端 Codex 未生成最终结论，不能视为修复完成。请核查本任务的服务器记录。'
        prefix = 'X 发布修复任务：CPU Codex 执行结果\n' if code == 0 else 'X 发布修复任务：CPU Codex 执行中断\n'
        message = prefix + body[:4800] + '\n\n服务器记录：' + str(directory)
        try:
            state['feishu_message_id'] = notify(args.feishu_config, args.recipient, message, state['message_uuid'])
            state['notification_state'] = 'delivered'
        except Exception as exc:
            state['notification_state'] = 'failed'
            state['notification_error_type'] = type(exc).__name__
        state['state'] = 'finished'
        write_json(state_path, state)
        if code or state['notification_state'] != 'delivered':
            raise SystemExit(1)


if __name__ == '__main__':
    main()
