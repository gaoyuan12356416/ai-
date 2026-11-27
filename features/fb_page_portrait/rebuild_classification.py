"""Reclassify verified, frozen income facts using the normal current-row publisher."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib

from .classification import classifications
from .collect import Reader, catalog, environment
from .reclassify import facts, reclassify
from .runtime import atomic_json, encoded
from .same_drama import language, verified_hotdrama_links
from .storage import ROOT, publish
from features.fb_page_current.common import current_state
from features.fb_page_current.read import request
from features.fb_page_current.refresh import primary, secondary


class Pages:
    def __init__(self, path):
        self.path = Path(path)

    def __len__(self):
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute('SELECT COUNT(*) FROM pages').fetchone()[0]

    def items(self):
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as db:
            for pid, packed in db.execute('SELECT page_id,payload FROM pages ORDER BY page_id'):
                yield pid, json.loads(zlib.decompress(packed))


def save_page(db, pid, profile):
    raw = encoded(profile).encode()
    db.execute('INSERT OR REPLACE INTO pages VALUES (?,?)', (pid, zlib.compress(raw, 3)))


def database(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS pages(page_id TEXT PRIMARY KEY,payload BLOB NOT NULL)')
    return db


def prepare(folder, root, reader):
    if (folder / 'prepared.json').exists():
        return json.loads((folder / 'prepared.json').read_text())
    pointer = json.loads((root / 'current.json').read_text())
    version = pointer['version']
    receipt = json.loads((root / (version + '.receipt.json')).read_text())
    saved = folder / 'original-pointer.json'
    if saved.exists() and json.loads(saved.read_text()) != pointer:
        raise RuntimeError('published_version_changed_use_new_work_directory')
    atomic_json(saved, pointer)
    atomic_json(folder / 'original-receipt.json', receipt)
    ids = sorted(receipt['payload_hashes'])
    with closing(database(folder / 'original.sqlite3')) as db:
        present = {r[0] for r in db.execute('SELECT page_id FROM pages')}
        def freeze(part):
            try:
                rows, meta = request(reader, version, part, detail=True, exact=True, step=len(part))
            except Exception as exc:
                if '3024' not in str(exc) or len(part) <= 1:
                    raise
                middle = len(part) // 2
                freeze(part[:middle]); freeze(part[middle:])
                return
            if set(rows) != set(part):
                raise RuntimeError('incomplete_frozen_pages')
            for pid, raw in rows.items():
                if hashlib.sha256(raw).hexdigest() != receipt['payload_hashes'][pid]:
                    raise RuntimeError('original_receipt_mismatch')
                save_page(db, pid, json.loads(raw))
            db.commit()
            atomic_json(folder / 'original-meta.json', meta)
        missing = [pid for pid in ids if pid not in present]
        for offset in range(0, len(missing), 24):
            freeze(missing[offset:offset + 24])
            print(encoded(dict(event='frozen_pages',done=min(offset+24,len(missing)),total=len(missing))), flush=True)
    meta = json.loads((folder / 'original-meta.json').read_text())
    original = Pages(folder / 'original.sqlite3')
    totals = dict.fromkeys(meta['audit'], 0)
    hot = set()
    for pid, profile in original.items():
        if hashlib.sha256(encoded(profile).encode()).hexdigest() != receipt['payload_hashes'][pid]:
            raise RuntimeError('frozen_backup_hash_mismatch')
        dramas, _ = facts(profile)
        hot.update(key for key in dramas if key[0] == 3302)
        for field in totals:
            totals[field] += profile[field]
    if totals != meta['audit'] or len(original) != meta['page_count']:
        raise RuntimeError('frozen_scope_or_money_mismatch')
    sources = folder / 'classification-inputs.json'
    if not sources.exists():
        collection = None
        for marker in (root / 'collect-cache').glob('*/manifest.json'):
            record = json.loads(marker.read_text())
            if record.get('complete') and record.get('started_at') == meta['collection_started_at']:
                collection = marker.parent
                break
        if collection is None:
            raise RuntimeError('confirmed_classification_collection_missing')
        infos, resources = [], []
        for file in collection.glob('*.json'):
            if file.name in ('manifest.json', 'social-input.json'):
                continue
            for row in json.loads(file.read_text()).get('rows', []):
                if 'series_code' not in row or row.get('app_id') not in (1479,3302):
                    continue
                if 'labels' in row: resources.append(row)
                elif 'resource_type' in row: infos.append(row)
        for table,index,columns,rows in [
            ('ads_drama_info','ac',['content_id','app_id','language','series_code','resource_type','name'],infos),
            ('ads_drama_resource','content_id',['content_id','app_id','language','series_code','labels'],resources),
        ]:
            present = {(r['app_id'],str(r['content_id']),language(r['language'])) for r in rows}
            needed = sorted({cid for _,cid,lang in hot if (1479,cid,lang) not in present})
            rows.extend(catalog(reader,table,index,columns,1479,needed))
        atomic_json(sources, dict(rows=infos+resources))
    source_rows = json.loads(sources.read_text())['rows']
    infos = [r for r in source_rows if 'resource_type' in r]
    resources = [r for r in source_rows if 'labels' in r]
    replacements = classifications(resources, infos)
    changed = []
    with closing(database(folder / 'candidate.sqlite3')) as db:
        for pid, profile in original.items():
            value = reclassify(profile, replacements)
            if value != profile: changed.append(pid)
            save_page(db,pid,value)
        db.commit()
    if not changed:
        raise RuntimeError('no_classification_changes')
    now = datetime.now(timezone.utc)
    new_version = now.strftime('%Y%m%dT%H%M%S%fZ')
    new_meta = dict(meta,version=new_version,generated_at=now.isoformat(),
        income_generated_at=meta.get('income_generated_at',meta['generated_at']),
        reclassified_from_version=version,
        classification_mapping_policy='hotdrama-same-id-language-series-v1',
        classification_input_file=new_version+'.classification-inputs.json',
        classification_input_sha256=hashlib.sha256(sources.read_bytes()).hexdigest())
    data = dict(meta=new_meta,changed_pages=changed,hot_drama_versions=len(hot),
                verified_links=len(hot & set(verified_hotdrama_links(infos))))
    atomic_json(folder / 'prepared.json',data)
    return data


def release(folder,root,env,reader):
    prepared = json.loads((folder / 'prepared.json').read_text())
    meta = prepared['meta']
    head = current_state(reader)
    if head['version'] not in (meta['reclassified_from_version'],meta['version']):
        raise RuntimeError('published_version_changed_reprepare_before_publish')
    raw = (folder / 'classification-inputs.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta['classification_input_sha256']:
        raise RuntimeError('classification_inputs_changed')
    atomic_json(root / meta['classification_input_file'],json.loads(raw))
    data = dict(meta=meta,pages=Pages(folder / 'candidate.sqlite3'))
    proof = primary(data,env,reader)
    atomic_json(root / (meta['version']+'.receipt.json'),proof)
    publish(data,proof,root)
    secondary(data,env,reader,proof)
    # Do not replace refresh status: income collection may still report stale inputs.
    atomic_json(folder / 'published.json',dict(version=meta['version'],verified_rows=proof['verified_rows'],audit=meta['audit']))
    return dict(version=meta['version'],changed_pages=len(prepared['changed_pages']),audit=meta['audit'])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','publish'])
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--root',type=Path,default=Path(ROOT))
    parser.add_argument('--env',default='/root/drama_material_service/.env')
    args=parser.parse_args()
    args.work=args.work.resolve()
    args.work.mkdir(parents=True,exist_ok=True)
    env=environment(args.env); reader=Reader(env)
    with (args.root / 'refresh.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=(prepare(args.work,args.root,reader) if args.action=='prepare'
                else release(args.work,args.root,env,reader))
    print(encoded(dict(event=args.action,**result)),flush=True)


if __name__ == '__main__': main()
