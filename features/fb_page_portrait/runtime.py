"""Use the deployed X taxonomy and social attribution as shared dependencies."""
import os
from pathlib import Path


def shared():
    import features
    path = str(Path(os.environ.get('FB_PORTRAIT_SHARED_ROOT', '/root/drama_material_service')) / 'features')
    if path not in features.__path__:
        features.__path__.append(path)


def encoded(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.next')
    with temp.open('w', encoding='utf-8') as handle:
        handle.write(encoded(value)); handle.flush(); os.fsync(handle.fileno())
    temp.replace(path)


def literal(value):
    return '0x' + str(value).encode().hex() if str(value) else "''"
