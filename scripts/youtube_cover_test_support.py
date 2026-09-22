"""Native artifact fixture for offline cover adapter tests."""
import json
from pathlib import Path
from types import SimpleNamespace
import uuid


def completed_generation(command, kwargs):
    work = Path(command[command.index('-C') + 1])
    thread_id = str(uuid.uuid4())
    directory = Path(kwargs['env']['CODEX_HOME']) / 'generated_images' / thread_id
    directory.mkdir(parents=True)
    path = work / 'cover.png'
    if path.is_file():
        (directory / 'exec-test.png').write_bytes(path.read_bytes())
    return SimpleNamespace(returncode=0, stdout=json.dumps({'type': 'thread.started', 'thread_id': thread_id}))
