"""Direct, operator-configured model inference. Accepts local images or text."""
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from .smart_common import SmartError, require


def model_config():
    return (os.environ.get('AI_VIDEO_EDITING_VISION_BASE_URL', '').strip(),
            os.environ.get('AI_VIDEO_EDITING_CODEX_BIN', '').strip(),
            os.environ.get('AI_VIDEO_EDITING_VISION_MODEL', '').strip())


def parse(text):
    text = str(text).strip()
    if text.startswith('```') and text.endswith('```'):
        text = text.partition('\n')[2].rsplit('```', 1)[0].strip()
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        raise SmartError('smart_model_invalid', '模型返回了无效的 JSON', 502) from None
    require(isinstance(result, dict), 'smart_model_invalid', '模型结果须为 JSON 对象', 502)
    return result


def model_json(prompt, images=(), workdir=None):
    base, binary, model = model_config()
    require(base or binary, 'smart_model_unconfigured', '智能剪辑模型尚未配置', 503)
    require(isinstance(prompt, str) and len(prompt) <= 100000, 'smart_model_prompt_invalid', '模型输入无效')
    images = [Path(p).resolve() for p in images]
    require(len(images) <= 12 and all(p.is_file() and p.stat().st_size <= 5 * 1024**2 for p in images),
            'smart_model_image_invalid', '模型图像输入无效')
    timeout = max(30, min(600, int(os.environ.get('AI_VIDEO_EDITING_MODEL_TIMEOUT', 210))))
    if base:
        import requests
        require(model, 'smart_model_unconfigured', '通用模型网关缺少模型名', 503)
        content = [{'type': 'text', 'text': prompt}]
        for path in images:
            mime = 'image/png' if path.suffix.lower() == '.png' else 'image/jpeg'
            raw = base64.b64encode(path.read_bytes()).decode('ascii')
            content.append({'type': 'image_url', 'image_url': {'url': 'data:%s;base64,%s' % (mime, raw)}})
        headers = {}
        key = os.environ.get('AI_VIDEO_EDITING_VISION_API_KEY')
        if key:
            headers['Authorization'] = 'Bearer ' + key
        try:
            response = requests.post(base.rstrip('/') + '/chat/completions', headers=headers,
                                     json={'model': model, 'messages': [{'role': 'user', 'content': content}]},
                                     timeout=(10, timeout), allow_redirects=False)
            require(response.status_code == 200, 'smart_model_unavailable', '模型网关调用未成功', 503)
            return parse(response.json()['choices'][0]['message']['content'])
        except (requests.RequestException, ValueError, IndexError, KeyError, TypeError) as exc:
            if isinstance(exc, SmartError):
                raise
            raise SmartError('smart_model_unavailable', '模型网关未返回有效响应', 503) from None
    with tempfile.TemporaryDirectory(prefix='ai_edit_model_', dir=workdir) as private:
        target = Path(private).resolve() / 'result.json'
        args = [binary, 'exec', '--skip-git-repo-check', '--sandbox', 'read-only', '-C', str(target.parent),
                '-c', 'approval_policy="never"', '-c', 'features.shell_tool=false',
                '-c', 'model_reasoning_effort="low"', '-o', str(target)]
        if model:
            args.extend(['-m', model])
        for path in images:
            args.extend(['-i', str(path)])
        args.append('-')
        allowed = ('PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TMPDIR', 'TMP', 'TEMP', 'SYSTEMROOT', 'APPDATA',
                   'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY')
        env = {k: os.environ[k] for k in allowed if k in os.environ}
        if os.environ.get('AI_VIDEO_EDITING_CODEX_HOME'):
            env['CODEX_HOME'] = os.environ['AI_VIDEO_EDITING_CODEX_HOME']
        try:
            proc = subprocess.Popen(args, cwd=private, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt')
            try:
                proc.communicate(prompt.encode('utf8'), timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.communicate()
                raise SmartError('smart_model_timeout', '模型调用超时', 503) from None
        except OSError:
            raise SmartError('smart_model_unavailable', '无法启动智能剪辑模型', 503) from None
        require(proc.returncode == 0 and target.is_file() and target.stat().st_size <= 1024**2,
                'smart_model_unavailable', '智能剪辑模型未返回有效结果', 503)
        return parse(target.read_text(encoding='utf8'))
