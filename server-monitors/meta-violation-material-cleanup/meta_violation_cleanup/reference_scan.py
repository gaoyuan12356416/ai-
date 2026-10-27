"""Bounded, read-only reference inventories with no worker ledger/SQL access.

Each account is owned by at most one worker, even when more than one frozen
credential is supplied for it. A failed page invalidates the entire inventory.
The caller alone decides eligibility and persists any observations.
"""
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import threading
import time

from .graph import AD_FIELDS, GraphClient, GraphError, _object_id
from .throttle import GovernedSession, RequestNotSent


class _ReadClient(GraphClient):
    """Keep local page/request counters alongside shared safe Graph counters."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reference_pages = 0
        self.reference_requests = 0

    def _get(self, *args, **kwargs):
        self.reference_requests += 1
        return super()._get(*args, **kwargs)

    def _pages(self, *args, **kwargs):
        for rows in super()._pages(*args, **kwargs):
            self.reference_pages += 1
            yield rows


def _new_reader(runner, governor, stop):
    original = runner.graph
    # Existing deterministic test/custom adapters have no network sessions.
    # Production GraphClient instances always get a separate connection pool.
    if not isinstance(original, GraphClient):
        return original, False
    if governor is None:
        raise ValueError('reference_scan_governor_required')
    reader = _ReadClient(original.tokens, original.version,
                         min(20, original.timeout), get_retries=0,
                         max_pages=original.max_pages,
                         max_objects=original.max_objects, read_workers=1)
    reader.share_read_metrics(original)
    reader.stop = stop
    reader.delete_stop = lambda: True
    reader.http = GovernedSession(reader.http, governor, stop)
    return reader, True


def _safe_error(exc):
    if isinstance(exc, GraphError):
        return exc.as_dict()
    # Never stringify arbitrary transport/custom exceptions: they may contain
    # headers, request URLs or credentials.
    return {'code': 'reference_read_unavailable', 'type': type(exc).__name__}


def _stopped(stop):
    if stop():
        raise InterruptedError('stop_requested')


def _keys(account_keys):
    groups = defaultdict(deque)
    seen = set()
    for account, user in account_keys:
        key = (_object_id(account, account=True), _object_id(user))
        if key not in seen:
            seen.add(key)
            groups[key[0]].append(key)
    return groups


def scan_accounts(runner, account_keys, governor, stop=lambda: False, metrics=None):
    """Yield ``((account, user), result)`` after each complete account read.

    ``metrics`` is an optional caller-owned dict, updated only on the caller
    thread. ``result['by_creative']`` is empty on every failure; per-account
    ``metrics`` and cumulative ``scan_metrics`` never contain credentials.
    Consume or close the iterator to drain workers and close their sessions.
    """
    workers = runner.config.get('shared_reference_scan_workers', 4)
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('shared_reference_scan_workers_out_of_range')
    grouped = _keys(account_keys)
    order = deque(grouped)
    cancelled = threading.Event()
    should_stop = lambda: cancelled.is_set() or stop() or runner.stop()
    started = time.monotonic()
    totals = {'account_scans': 0, 'accounts_completed': 0,
              'account_scan_failures': 0, 'pages': 0, 'peak_accounts': 0,
              'wall_seconds': 0.0}
    active = 0
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = {}

    def snapshot():
        with lock:
            output = dict(totals, wall_seconds=round(time.monotonic()-started, 3))
        if metrics is not None:
            metrics.clear()
            metrics.update(output)
        return output

    def read(key):
        nonlocal active
        reader = None
        owned = False
        began = time.monotonic()
        _stopped(should_stop)
        with lock:
            active += 1
            totals['account_scans'] += 1
            totals['peak_accounts'] = max(totals['peak_accounts'], active)
        result = {'complete': False, 'by_creative': {}}
        try:
            reader, owned = _new_reader(runner, governor, should_stop)
            if hasattr(getattr(reader, 'http', None), 'account'):
                reader.http.account = key[0]
            rows = reader.list_ads(*key)
            _stopped(should_stop)
            by_creative = defaultdict(list)
            for ad in rows:
                if not isinstance(ad, dict) or str(ad.get('account_id')) != key[0]:
                    raise GraphError('account_mismatch', 'Reference account mismatch')
                _object_id(ad.get('id'))
                # An unknown binding could hide an active reference. Do not
                # silently drop it and declare this inventory complete.
                creative = _object_id((ad.get('creative') or {}).get('id'))
                by_creative[creative].append(ad)
            result = {'complete': True, 'by_creative': dict(by_creative)}
        except (InterruptedError, RequestNotSent):
            raise InterruptedError('stop_requested') from None
        except Exception as exc:
            result['error'] = _safe_error(exc)
        finally:
            pages = int(getattr(reader, 'reference_pages', 0)) if reader else 0
            result['metrics'] = {'pages': pages,
                                 'account_seconds': round(time.monotonic()-began, 3)}
            if owned:
                reader.close()
            with lock:
                active -= 1
                totals['pages'] += pages
                totals['accounts_completed' if result['complete'] else 'account_scan_failures'] += 1
        return result

    def fill():
        while order and len(pending) < workers:
            _stopped(should_stop)
            account = order.popleft()
            key = grouped[account].popleft()
            pending[pool.submit(read, key)] = key

    try:
        fill()
        while pending:
            _stopped(should_stop)
            done, _ = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
            for future in done:
                key = pending.pop(future)
                result = future.result()
                if grouped[key[0]]:
                    order.append(key[0])
                result['scan_metrics'] = snapshot()
                yield key, result
            fill()
    finally:
        cancelled.set()
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True)
        snapshot()


def read_known_ads(runner, account_id, user_id, ad_ids, governor, stop=lambda: False):
    """Targeted GETs of known blockers, at most 50 IDs per request.

    Only explicit object errors 100/803 may bisect a request. Permission,
    transport and rate-limit failures do not fan out or rotate credentials.
    Missing/unreadable IDs remain errors, never evidence of a cleared binding.
    """
    account = _object_id(account_id, account=True)
    user = _object_id(user_id)
    identifiers = list(dict.fromkeys(_object_id(value) for value in ad_ids))
    should_stop = lambda: stop() or runner.stop()
    records, errors = {}, {}
    began = time.monotonic()
    if not identifiers:
        return {'records': records, 'errors': errors,
                'metrics': {'target_ids': 0, 'requests': 0, 'wall_seconds': 0.0}}
    _stopped(should_stop)
    reader, owned = _new_reader(runner, governor, should_stop)
    requests = 0
    try:
        if hasattr(getattr(reader, 'http', None), 'account'):
            reader.http.account = account
        for offset in range(0, len(identifiers), 50):
            parts = [identifiers[offset:offset+50]]
            while parts:
                _stopped(should_stop)
                part = parts.pop()
                try:
                    requests += 1
                    if not isinstance(reader, GraphClient) and not hasattr(reader, '_get'):
                        # Custom adapters used by deterministic execution tests.
                        data = {identifier: reader.get_ad(identifier, user) for identifier in part}
                    else:
                        data = reader._get('', user, {'ids': ','.join(part), 'fields': AD_FIELDS})
                except GraphError as exc:
                    if len(part) > 1 and exc.code in (100, 803):
                        middle = len(part)//2
                        parts.extend((part[middle:], part[:middle]))
                        continue
                    errors.update({identifier: _safe_error(exc) for identifier in part})
                    continue
                except (InterruptedError, RequestNotSent):
                    raise InterruptedError('stop_requested') from None
                except Exception as exc:
                    errors.update({identifier: _safe_error(exc) for identifier in part})
                    continue
                for identifier in part:
                    try:
                        value = data.get(identifier) if isinstance(data, dict) else None
                        if isinstance(value, dict) and value.get('error'):
                            error = value['error']
                            if not isinstance(error, dict):
                                raise GraphError('invalid_object', 'Known reference error is malformed')
                            errors[identifier] = {'code': error.get('code'),
                                                  'error_subcode': error.get('error_subcode')}
                            continue
                        if isinstance(reader, GraphClient):
                            value = reader._record(value, identifier, account)
                        elif (not isinstance(value, dict) or str(value.get('id')) != identifier
                              or str(value.get('account_id')) != account):
                            raise GraphError('object_mismatch', 'Known reference identity mismatch')
                        records[identifier] = value
                    except GraphError as exc:
                        errors[identifier] = _safe_error(exc)
        _stopped(should_stop)
    finally:
        if owned:
            reader.close()
    return {'records': records, 'errors': errors,
            'metrics': {'target_ids': len(identifiers), 'requests': requests,
                        'wall_seconds': round(time.monotonic()-began, 3)}}
