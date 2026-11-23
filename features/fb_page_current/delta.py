"""Temporary durable retry input: only changed Pages carry their full JSON."""
import gzip
import json
import os
from .common import checked,encoded,sha


def freeze(path,source,prior):
    temp=path.with_suffix('.building')
    with temp.open('wb') as out:
        with gzip.GzipFile(fileobj=out,mode='wb',compresslevel=1,mtime=0) as gz:
            for pid,summary,raw,digest,amounts,when in source:
                if sha(raw)!=digest:raise ValueError('unverified delta input')
                detail=None if prior.get(pid)==digest else raw.decode()
                record=[pid,summary,detail,digest,amounts,str(when),len(raw)]
                gz.write(encoded(record).encode()+b'\n')
        out.flush();os.fsync(out.fileno())
    temp.replace(path)


def read(path):
    with gzip.open(path,'rb') as gz:
        for line in gz:
            pid,summary,detail,digest,amounts,when,length=json.loads(line)
            yield pid,summary,None if detail is None else detail.encode(),digest,amounts,when,length
