"""Read a single exact Page from the immutable pre-conversion backup."""
import gzip
import json
import re
from .common import ROOT,PAGE,checked,sha,unpack

HISTORY=ROOT/'history'
BLOB=re.compile('[0-9a-f]{64}')


def blob_path(digest,base=HISTORY):
    if not BLOB.fullmatch(digest):raise ValueError('invalid archive digest')
    return base/'blobs'/digest[:2]/digest


def rows(file):
    with gzip.open(file,'rt',encoding='utf-8') as src:
        for line in src:yield json.loads(line)


def lookup(v,p,base=HISTORY):
    checked(v);checked(p,PAGE)
    d=base/'versions'/v
    manifest=d/'snapshot.json'
    if not manifest.exists():raise FileNotFoundError('snapshot_expired')
    m=json.loads(manifest.read_text())
    if m['version']!=v:raise ValueError('invalid historical version')
    for chunk in m['parts']:
        if chunk['first']<=p<=chunk['last']:
            f=d/chunk['name']
            if sha(f.read_bytes())!=chunk['sha256']:raise ValueError('archive chunk verification failed')
            for row in rows(f):
                if row[1]==p:
                    raw=unpack(blob_path(row[9],base).read_bytes())
                    if sha(raw)!=row[9]:raise ValueError('archive payload verification failed')
                    return raw,json.loads(row[8])
            break
    return None,json.loads(m['meta'])
