"""Bounded, local thumbnail cache. Original images are never browser dependencies."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import io
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import warnings

ALLOWED_HOSTS = frozenset({'advertising-1306474899.cos.ap-hongkong.myqcloud.com'})
PRODUCTS = ('1479', '2373', '3360')
MAX_DOWNLOAD = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
TIME_BUDGET = 90
MAX_WORKERS = 4


def normalize_url(value):
    value = str(value or '').strip()
    if value.startswith('//'):
        value = 'https:' + value
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or parsed.hostname not in ALLOWED_HOSTS or
            parsed.username or parsed.password or parsed.port not in (None, 443) or not parsed.path):
        raise ValueError('thumbnail_url_not_allowed')
    # Do not persist tokens, signed query parameters, fragments, or insecure URLs.
    return urlunsplit(('https', parsed.hostname, parsed.path, '', ''))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('thumbnail_redirect_not_allowed')


def download(url, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('thumbnail_budget_exhausted')
    request = Request(url, headers={'Accept': 'image/*', 'User-Agent': 'AIImageDailyThumbnail/3'})
    with build_opener(NoRedirect()).open(request, timeout=min(10, remaining)) as response:
        if response.status != 200:
            raise ValueError('thumbnail_http_status')
        length = response.headers.get('Content-Length')
        if length and int(length) > MAX_DOWNLOAD:
            raise ValueError('thumbnail_download_too_large')
        data = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('thumbnail_budget_exhausted')
            chunk = response.read(min(65536, MAX_DOWNLOAD + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_DOWNLOAD:
                raise ValueError('thumbnail_download_too_large')
        return bytes(data)


def encode_thumbnail(data):
    from PIL import Image, ImageOps
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > MAX_PIXELS:
                raise ValueError('thumbnail_image_too_large')
            original.seek(0)
            original.thumbnail((160, 160))
            image = ImageOps.exif_transpose(original).convert('RGB')
            target = io.BytesIO()
            image.save(target, 'WEBP', quality=72, method=4)
            return target.getvalue()


def cache_one(url, cache, deadline, fetch=download):
    identity = hashlib.sha256(url.encode('utf-8')).hexdigest()
    target = cache / (identity + '.webp')
    if target.exists():
        # Cache files are generated locally; inspect before exposing cached bytes.
        data = target.read_bytes()
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            if img.format != 'WEBP' or max(img.size) > 160:
                raise ValueError('thumbnail_cache_invalid')
        return data, True
    if time.monotonic() >= deadline:
        raise TimeoutError('thumbnail_budget_exhausted')
    data = encode_thumbnail(fetch(url, deadline))
    if time.monotonic() >= deadline:
        raise TimeoutError('thumbnail_budget_exhausted')
    temporary = target.with_suffix('.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return data, False


def choose_materials(report):
    materials = report.get('views', {}).get('all', {}).get('materials', report.get('materials', []))
    result = {}
    for product in PRODUCTS:
        rows = report.get('views', {}).get(product, {}).get('materials', [])
        ordered = sorted(rows, key=lambda row: (-float(row.get('spend') or 0), str(row['id'])))
        top = [row for row in ordered if float(row.get('spend') or 0) > 0][:25]
        fresh = [row for row in materials if str(row.get('source_product_id', '')) == product and
                 str(row.get('created_at', ''))[:10] == report['report_date']]
        spends = {str(row['id']): float(row.get('spend') or 0) for row in rows}
        fresh.sort(key=lambda row: (-spends.get(str(row['id']), 0), str(row['id'])))
        selected, seen = [], set()
        for row in top + fresh:
            identity = str(row['id'])
            if identity not in seen:
                selected.append(row)
                seen.add(identity)
        result[product] = selected[:100]
    return result


def prepare_thumbnails(report, cache, base_url, budget=TIME_BUDGET, fetch=download):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    selected = choose_materials(report)
    failures, urls, candidates = [], {}, []
    # Round-robin products keeps slow requests from starving the third product.
    for index in range(max((len(rows) for rows in selected.values()), default=0)):
        for product in PRODUCTS:
            if index >= len(selected[product]):
                continue
            row = selected[product][index]
            try:
                url = normalize_url(row.get('url'))
            except (ValueError, TypeError):
                failures.append(dict(id=str(row['id']), product_id=product, reason='url_not_allowed'))
                continue
            if url not in urls:
                candidates.append(url)
                urls[url] = []
            urls[url].append(str(row['id']))
    artifacts, resolved, cached = {}, {}, 0
    started = time.monotonic()
    # Reserve one maximum socket timeout for draining active workers. Queue
    # dispatch ends earlier, keeping the whole thumbnail stage within 90s.
    deadline = started + max(0, float(budget) - min(10, float(budget)))
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    pending, iterator = {}, iter(candidates)

    def submit():
        while len(pending) < MAX_WORKERS and time.monotonic() < deadline:
            url = next(iterator, None)
            if url is None:
                break
            pending[pool.submit(cache_one, url, cache, deadline, fetch)] = url

    try:
        submit()
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                url = pending.pop(future)
                try:
                    data, hit = future.result()
                    cached += int(hit)
                    key = 'assets/thumbs/' + hashlib.sha256(data).hexdigest() + '.webp'
                    artifacts[key] = dict(data=data, content_type='image/webp', immutable=True, compress=False)
                    for identity in urls[url]:
                        resolved[identity] = base_url.rstrip('/') + '/' + key
                except Exception as error:
                    # Keep source URLs and response bodies out of diagnostics.
                    failures.extend(dict(id=identity, reason=type(error).__name__) for identity in urls[url])
            submit()
        for future, url in pending.items():
            future.cancel()
            failures.extend(dict(id=identity, reason='time_budget') for identity in urls[url])
        for url in iterator:
            failures.extend(dict(id=identity, reason='time_budget') for identity in urls[url])
    finally:
        # Active sockets have <=10s deadlines; no queued work survives the budget.
        pool.shutdown(wait=True, cancel_futures=True)
    def decorate(value):
        if isinstance(value, dict):
            if 'id' in value and 'url' in value:
                value['thumbnail_url'] = resolved.get(str(value['id']), '')
            for child in value.values():
                decorate(child)
        elif isinstance(value, list):
            for child in value:
                decorate(child)
    decorate(report)
    status = dict(selected_by_product={key: len(rows) for key, rows in selected.items()},
                  resolved_materials=len(resolved), artifacts=len(artifacts), cached=cached,
                  failures=failures, max_per_product=100, max_workers=MAX_WORKERS,
                  time_budget_seconds=budget, thumbnail_max_pixels=160)
    status['elapsed_seconds'] = round(time.monotonic() - started, 3)
    (cache / 'last-result.json').write_text(json.dumps(status, ensure_ascii=False), encoding='utf-8')
    return artifacts, status
