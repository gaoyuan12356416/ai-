"""Resume completed SELECT inputs of one frozen portrait collection."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from .runtime import atomic_json,encoded

ROOT='/mnt/data-disk/fb-page-portraits/collect-cache'


class PartitionCache:
    def __init__(self,scope,start,end,root=ROOT):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        key=hashlib.sha256(encoded([scope,start,end,'historical-select-v1']).encode()).hexdigest()
        pointer=self.root/(key+'.json')
        current=json.loads(pointer.read_text()) if pointer.exists() else None
        if current:
            manifest=self.root/current['generation']/'manifest.json'
            state=json.loads(manifest.read_text()) if manifest.exists() else {}
            if state.get('complete'):current=None
        if not current:
            current=dict(generation=uuid.uuid4().hex,started_at=datetime.now(timezone.utc).isoformat(),complete=False)
            folder=self.root/current['generation'];folder.mkdir(mode=0o700)
            atomic_json(folder/'manifest.json',current);atomic_json(pointer,current)
        self.folder=self.root/current['generation']
        self.state=json.loads((self.folder/'manifest.json').read_text())

    def query(self,reader,statements,timeout=45):
        key=hashlib.sha256(encoded(statements).encode()).hexdigest()
        path=self.folder/(key+'.json')
        if path.exists():return json.loads(path.read_text())['rows']
        for attempt in range(2):
            try:
                rows=reader.query(statements,timeout=timeout)
                break
            except Exception as exc:
                transient=any(value in str(exc) for value in ('2013','2002','2003','2006','source_query_timeout','rc_124','rc_137','rc_143','rc_75'))
                if attempt or not transient:raise
                print(encoded(dict(event='historical_partition_read_retry',query_hash=key)),flush=True)
        atomic_json(path,dict(read_at=datetime.now(timezone.utc).isoformat(),rows=rows))
        return rows

    def metadata(self):return dict(self.state,manifest=str(self.folder/'manifest.json'))


def complete(raw):
    metadata=raw.get('collection') or {}
    if not metadata.get('manifest'):return
    path=Path(metadata['manifest']).resolve();root=path.parent.parent
    if path.name!='manifest.json' or len(path.parent.name)!=32 or path.parent.parent.name!='collect-cache':
        raise RuntimeError('invalid_partition_cache_manifest')
    state=json.loads(path.read_text());state['complete']=True;atomic_json(path,state)
    # Only disposable, fully published SELECT caches are pruned. The immutable
    # source inputs, profile versions, receipts and incomplete caches stay intact.
    completed=[]
    for folder in root.iterdir():
        manifest=folder/'manifest.json'
        if folder.is_dir() and len(folder.name)==32 and manifest.exists():
            item=json.loads(manifest.read_text())
            if item.get('complete'):completed.append((item.get('started_at',''),folder))
    for _,folder in sorted(completed,reverse=True)[2:]:
        if folder.resolve().parent==root:shutil.rmtree(folder)
