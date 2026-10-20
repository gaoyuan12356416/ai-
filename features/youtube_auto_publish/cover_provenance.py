"""Bind accepted image bytes to this isolated Codex invocation."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from .templates import WorkflowError


def isolated_home(work):
    home = work / 'codex-home'
    home.mkdir(mode=0o700)
    (home / 'generated_images').mkdir(mode=0o700)
    # Authentication is needed by the CLI; never inherit histories, images,
    # user instructions, plugins or global generation configuration.
    source = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    for name in ('auth.json', 'installation_id', 'models_cache.json'):
        path = source / name
        if path.is_file():
            shutil.copyfile(path, home / name)
            os.chmod(home / name, 0o600)
    return home


def remove_private_auth(home):
    (home / 'auth.json').unlink(missing_ok=True)


def verify_generated_origin(stdout, home, raw, started_at, generation_root):
    """Use the CLI's thread id and its fresh native artifact, never final prose."""
    def fail():
        raise WorkflowError('cover_generation_origin_unverified',
                            '封面缺少本次生图的来源凭证，已拦截历史图片；请重新生成或手动上传', 503)
    thread_ids = []
    for line in str(stdout or '').splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get('type') == 'thread.started':
            thread_ids.append(event.get('thread_id'))
    if len(thread_ids) != 1 or not isinstance(thread_ids[0], str) or not re.fullmatch(r'[a-f0-9-]{36}', thread_ids[0]):
        fail()
    thread_id = thread_ids[0]
    directory = home / 'generated_images' / thread_id
    if any(p.is_symlink() for p in (home, home / 'generated_images', directory)) or not directory.is_dir():
        fail()
    digest = hashlib.sha256(raw).hexdigest()
    matches = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            continue
        stat = path.stat()
        if stat.st_size != len(raw) or stat.st_mtime < started_at - 1:
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            matches.append(path)
    if not matches:
        fail()
    # Even a freshly copied file in the isolated home cannot resurrect an
    # accepted image from an older attempt or another task.
    for audit_path in generation_root.glob('*/*/generation-output.json'):
        try:
            audit = json.loads(audit_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            fail()
        if audit.get('source_sha256') == digest:
            raise WorkflowError('cover_generation_reused_image',
                                '本次封面与历史生图完全相同，已拦截复用；请重新生成或手动上传', 503)
    return {'policy': 'isolated_thread_artifact_v1', 'thread_id': thread_id,
            'artifact': str(matches[0].relative_to(home)), 'sha256': digest}
