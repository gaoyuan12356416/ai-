"""Stream a receipted FB generation through the read-only replica and the audience catalogue."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
import zlib

from . import reproject, store


MONEY = ('paid_cents', 'social_cents', 'spend_cents')
TABLE = 'ads_ai.fb_page_revenue_profile'


def source(root, manifest, reader=None):
    version = manifest['version']
    if manifest.get('database') != TABLE or not re.fullmatch(r'[0-9]{8}T[0-9]{12}Z', version):
        raise ValueError('invalid FB source')
    if reader is None:
        from features.social_analytics.warehouse_refresh import Reader, environment
        reader = Reader(environment('/root/drama_material_service/.env'))
    receipt = json.loads((root/(version+'.receipt.json')).read_text())
    hashes = receipt['payload_hashes']
    if receipt['version'] != version or len(hashes) != receipt['verified_rows'] or not hashes:
        raise ValueError('incomplete FB receipt')
    if not all(re.fullmatch(r'[1-9][0-9]{0,31}', key) for key in hashes):
        raise ValueError('invalid receipted Page')
    where = f" FROM {TABLE} WHERE profile_version='{version}'"
    rows = reader.query(['SELECT HEX(meta_json)'+where+' LIMIT 1'], timeout=45)
    if not rows or rows[0]['version'] != version:
        raise RuntimeError('FB version changed during preview')
    meta = rows[0]
    if meta['page_count'] != len(hashes) or any(meta['audit'][k] != receipt[k] for k in MONEY):
        raise ValueError('FB metadata and receipt differ')

    def pages():
        ids = sorted(hashes)
        totals = Counter()
        for start in range(0, len(ids), 48):
            batch = ids[start:start+48]
            condition = ' AND page_id IN ('+','.join("'"+pid+"'" for pid in batch)+')'
            query = ("SELECT HEX(JSON_OBJECT('id',page_id,'body',HEX(COMPRESS(detail_json))))"+where+condition)
            rows = reader.query([query], timeout=90)
            if len(rows) != len(batch) or {r['id'] for r in rows} != set(batch):
                raise RuntimeError('FB version changed during preview')
            for row in rows:
                pid = row['id']
                compressed = bytes.fromhex(row['body'])
                raw = zlib.decompress(compressed[4:])
                if len(raw) != int.from_bytes(compressed[:4],'little') or hashlib.sha256(raw).hexdigest() != hashes[pid]:
                    raise ValueError('FB detail checksum mismatch')
                page = json.loads(raw)
                if page['page_id'] != pid:
                    raise ValueError('FB Page mismatch')
                totals.update({k: page[k] for k in MONEY})
                yield pid, page
        if any(totals[k] != receipt[k] for k in MONEY):
            raise ValueError('FB financial totals differ')
    return meta, pages()


def line(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


@contextmanager
def page_file(path, meta):
    """Keep standard JSON output, with one independently readable Page per line."""
    with path.open('w', encoding='utf-8', newline='\n') as out:
        out.write('{"meta":'+line(meta)+',"pages":{\n')
        first = True
        def add(pid, page):
            nonlocal first
            if not first:
                out.write(',\n')
            first = False
            out.write(line(pid)+':'+line(page))
        yield add
        out.write('\n}}\n')


def preview(folder, root, pointer):
    meta, pages = source(root, pointer)
    now = datetime.now(timezone.utc)
    next_meta = dict(meta, version=now.strftime('%Y%m%dT%H%M%S%fZ'), generated_at=now.isoformat(),
                     audience_source='material_list2.audience_labels', audience_taxonomy_version=store.VERSION,
                     taxonomy_version=store.VERSION, projection_only=True)
    cache, looked_up, changes = {}, set(), []
    count = revenue = 0
    with page_file(folder/'fb-before.json', meta) as before, page_file(folder/'fb-next.json', next_meta) as after:
        for pid, page in pages:
            ids = {cid for app,cid,lang in reproject.facts(page) if app == 1479}
            missing = ids-looked_up
            if missing:
                cache.update(store.records(missing))
                looked_up.update(missing)
            updated = reproject.rebuild(page, cache)
            old = {a['key']:a['revenue_cents'] for a in page['audiences']}
            new = {a['key']:a['revenue_cents'] for a in updated['audiences']}
            if old != new:
                changes.append(dict(channel=pid, before=old, after=new))
            before(pid, page)
            after(pid, updated)
            revenue += page['revenue_cents']
            count += 1
    return dict(channels=count, changed_channels=len(changes), changes=changes, drama_ids=len(looked_up),
                stored=len(cache),missing=sorted(looked_up-cache.keys()), revenue_cents=revenue,
                status_counts=dict(Counter(r['status'] for r in cache.values())),taxonomy_version=store.VERSION)
