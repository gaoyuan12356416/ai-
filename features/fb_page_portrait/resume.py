"""Reconstruct an interrupted immutable snapshot using its original version key."""
from datetime import datetime,timezone
import json
from pathlib import Path
import re
from .build import build
from .storage import VERSION_RE


def restore(root,path):
    root=Path(root).resolve();path=Path(path).resolve()
    match=re.fullmatch('('+VERSION_RE+r')\.input\.json',path.name)
    if path.parent!=root or not match:raise ValueError('invalid_resume_input')
    version=match.group(1)
    if (root/'current.json').exists():
        current=json.loads((root/'current.json').read_text())['version']
        if current>=version:raise ValueError('snapshot_already_published_or_superseded')
    raw=json.loads(path.read_text())
    data=build(raw)
    data['meta']['version']=version
    data['meta']['generated_at']=datetime.strptime(version,'%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc).isoformat()
    return raw,data
