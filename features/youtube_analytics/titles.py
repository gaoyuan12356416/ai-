"""Optional current English titles. Never participate in metric attribution."""
import threading
import time
from collections import OrderedDict, defaultdict

BATCH_SIZE = 100
_cache = OrderedDict()
_lock = threading.Lock()


def literal(value):
    return 'CONVERT(0x' + value.encode('utf-8').hex() + ' USING utf8mb4) COLLATE utf8mb4_unicode_ci'


def resolve(content_ids, query):
    ids = sorted(set(content_ids))
    if len(ids) > 2000 or any(not isinstance(i, str) or not 0 < len(i) <= 256 for i in ids):
        raise ValueError('title_lookup_limit')
    deadline = time.monotonic() + 25
    series = defaultdict(set)
    names = defaultdict(set)
    for start in range(0, len(ids), BATCH_SIZE):
        if time.monotonic() > deadline:
            raise ValueError('title_lookup_timeout')
        batch = ids[start:start+BATCH_SIZE]
        rows = query("SELECT DISTINCT HEX(JSON_OBJECT('content_id',content_id,'series_code',series_code)) "
                     "FROM kunlunads_dev.ads_drama_info WHERE app_id=1479 AND content_id IN (" +
                     ','.join(map(literal, batch)) + ') LIMIT 1001')
        if len(rows) > 1000:
            raise ValueError('title_source_limit')
        for row in rows:
            content, code = str(row['content_id']), str(row.get('series_code') or '').strip()
            if content in batch and code and code != '0':
                series[content].add(code)
    codes = sorted({next(iter(s)) for s in series.values() if len(s) == 1})
    for start in range(0, len(codes), BATCH_SIZE):
        if time.monotonic() > deadline:
            raise ValueError('title_lookup_timeout')
        batch = codes[start:start+BATCH_SIZE]
        rows = query("SELECT DISTINCT HEX(JSON_OBJECT('series_code',series_code,'name',TRIM(name))) "
                     "FROM kunlunads_dev.ads_drama_info WHERE app_id=1479 AND language='en' AND series_code IN (" +
                     ','.join(map(literal, batch)) + ') LIMIT 1001')
        if len(rows) > 1000:
            raise ValueError('title_name_limit')
        for row in rows:
            code, name = str(row['series_code']), str(row.get('name') or '').strip()
            if code in batch and name:
                names[code].add(name)
    result = {}
    for content in ids:
        codes = series[content]
        titles = names[next(iter(codes))] if len(codes) == 1 else set()
        status = 'matched' if len(titles) == 1 else 'ambiguous' if len(codes) > 1 or len(titles) > 1 else 'missing'
        result[content] = {'drama_english_name': next(iter(titles)) if status == 'matched' else '',
                           'drama_english_status': status}
    return result


def enrich(report, query, *, export=False):
    if 'drama' not in report['group_by']:
        return
    rows = report['all_rows'] if export else report['rows']
    ids = sorted({row['drama'] for row in rows})
    values = {}
    if _lock.acquire(timeout=1):
        try:
            now = time.monotonic()
            missing = [i for i in ids if i not in _cache or _cache[i][0] <= now]
            if missing:
                try:
                    fresh = resolve(missing, query)
                except Exception:
                    fresh = {i: {'drama_english_name': '', 'drama_english_status': 'unavailable'} for i in missing}
                for i, value in fresh.items():
                    _cache[i] = (now + (10 if value['drama_english_status'] == 'unavailable' else 300), value)
            for i in ids:
                values[i] = _cache[i][1]
                _cache.move_to_end(i)
            while len(_cache) > 2048:
                _cache.popitem(last=False)
        finally:
            _lock.release()
    for row in rows:
        row.update(values.get(row['drama'], {'drama_english_name': '', 'drama_english_status': 'unavailable'}))
    if any(row['drama_english_status'] == 'unavailable' for row in rows):
        report['warnings'].append('英文剧名暂时读取失败，效果数据仍按原归因统计。')


def display_title(row):
    return row.get('drama_english_name') or {'ambiguous': '英文名待核实', 'unavailable': '英文名暂不可用'}.get(
        row.get('drama_english_status'), '暂无英文名')
