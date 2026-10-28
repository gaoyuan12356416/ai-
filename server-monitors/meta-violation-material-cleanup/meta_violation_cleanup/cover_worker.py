"""Bounded cover replacement and one-attempt old-Creative cleanup.

SQL preparation belongs to the caller/owner thread. Network workers use their
own Graph client, one shared governor, and transaction-scoped durable claims.
This module never deletes an Ad or retries a sent mutation.
"""
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from copy import deepcopy
import hashlib
import threading
import time

from .cover_payload import (PayloadError, build_payload, payload_key,
                            verify_ad_binding, verify_creative)
from .cover_store import CoverStoreError
from .graph import GraphError
from .policy import _feedback_empty
from .throttle import RequestNotSent


_WAITING = {'DISAPPROVED', 'PENDING_REVIEW', 'IN_PROCESS', 'PREAPPROVED'}
_CLEAR = {'ACTIVE', 'PAUSED', 'ADSET_PAUSED', 'CAMPAIGN_PAUSED',
          'ARCHIVED', 'WITH_ISSUES', 'PENDING_BILLING_INFO'}
_DEFER_CODES = {'cover_stopped', 'cover_context_expired', 'cancelled_before_request',
                'cancelled_before_write', 'cover_shared_busy', 'cover_resource_busy'}


def _error(exc):
    if isinstance(exc, GraphError):
        return exc.as_dict()
    return {'code': str(getattr(exc, 'code', type(exc).__name__))}


def _reason(result):
    error = result.get('error') or {}
    return str(result.get('code') or error.get('code') or '')


def _binding(ad):
    return str((ad.get('creative') or {}).get('id') or '')


def review_state(ad):
    """Historical violations do not disqualify a currently clear Ad."""
    status = str(ad.get('status') or '').upper()
    effective = str(ad.get('effective_status') or '').upper()
    if 'DELETED' in (status, effective):
        return 'obsolete', 'ad_deleted'
    if status in _WAITING or effective in _WAITING:
        return 'waiting_review', 'review_not_cleared'
    try:
        clear = all(_feedback_empty(ad.get(k)) for k in ('issues_info', 'ad_review_feedback'))
    except (TypeError, ValueError):
        return 'waiting_review', 'review_unreadable'
    if not clear:
        return 'waiting_review', 'current_review_issue'
    if status not in ('ACTIVE', 'PAUSED', 'ARCHIVED') or effective not in _CLEAR:
        return 'waiting_review', 'current_state_unconfirmed'
    return 'ready', ''


def _fair(rows):
    queues = defaultdict(deque)
    for row in rows:
        queues[str(row['account_id'])].append(row)
    while queues:
        for account in list(queues):
            yield queues[account].popleft()
            if not queues[account]:
                del queues[account]


class CoverWorker:
    def __init__(self, config, store, source, graph_factory, shared_store,
                 stop=lambda: False, clock=time.monotonic):
        self.config, self.store, self.source = config, store, source
        self.graph_factory, self.shared = graph_factory, shared_store
        self.external_stop, self.clock = stop, clock
        self.halted = threading.Event()
        self.halt_reason = ''
        self.local = threading.local()
        self.mutex = threading.Lock()
        self.clients, self.resource_locks, self.verified_resources = [], {}, set()
        self.workers = int(config.get('execution_workers', 32))
        self.batch_size = int(config.get('execution_batch_size', 64))
        self.ttl = float(config.get('execution_context_ttl_seconds', 30))
        if not 1 <= self.workers <= 32 or not 1 <= self.batch_size <= 256 or not 1 <= self.ttl <= 120:
            raise ValueError('unsafe_cover_execution_limits')

    def stopped(self):
        return self.halted.is_set() or self.external_stop()

    def _halt(self, reason):
        with self.mutex:
            if not self.halt_reason:
                self.halt_reason = reason
            self.halted.set()

    def _graph(self):
        if not hasattr(self.local, 'graph'):
            self.local.graph = self.graph_factory()
            with self.mutex:
                self.clients.append(self.local.graph)
        return self.local.graph

    def close(self):
        for graph in self.clients:
            graph.close()
        self.clients = []

    def recover_reservations(self):
        """Recover commit-before-release crashes under the exclusive worker lock.

        The caller must first run CoverStore.recover_inflight(). No network
        mutation occurs here. Unknown Ad bindings and delete intents remain
        fenced unless a durable, confirmed result already resolves them.
        """
        owners = defaultdict(list)
        for reservation in self.shared.cover_reservations():
            owners[reservation['owner']].append(reservation)
        counts = Counter()
        for owner, reservations in owners.items():
            if owner.startswith('cover-task:'):
                suffix = owner[len('cover-task:'):]
                if not suffix.isdigit():
                    continue
                task = self.store.get_task(int(suffix))
                if not task:
                    continue
                result = task['result']
                confirmed = task['status'] == 'succeeded' and result.get('confirmed_binding') is True
                unsent = not task.get('request_sent') and task['status'] in (
                    'pending', 'failed', 'obsolete', 'blocked', 'waiting_review')
                rejected = task['status'] == 'failed' and (result.get('bind_result') or {}).get('status') == 'failed'
                if not confirmed and (any(r['status'] == 'unknown' for r in reservations) or not (unsent or rejected)):
                    counts['retained'] += 1
                    continue
                self.shared.release_cover_resources(owner, verified=confirmed)
                counts['task_reservations_released'] += 1
            elif owner.startswith('cover-delete:'):
                parts = owner.split(':')
                if len(parts) != 3 or not all(part.isdigit() for part in parts[1:]):
                    continue
                account, creative = parts[1:]
                row = self.store.get_delete(account, creative)
                if not row:
                    continue
                status, result = row['status'], row['result']
                confirmed = status in ('deleted_verified', 'previous_deleted') and result.get('confirmed_deleted') is True
                definite = confirmed or status in ('failed', 'blocked')
                guard = self.shared.get_cover_delete_once(account, creative)
                if not definite or guard and guard['owner'] != owner:
                    counts['retained'] += 1
                    continue
                if guard and guard['status'] in ('in_progress', 'unknown'):
                    if guard['status'] == 'unknown' and not confirmed:
                        counts['retained'] += 1
                        continue
                    self.shared.finish_cover_delete(owner, account, creative, status, result)
                    counts['delete_receipts_completed'] += 1
                self.shared.release_cover_resources(owner, verified=confirmed)
                counts['delete_reservations_released'] += 1
        return dict(counts)

    def _before(self, context):
        if self.stopped():
            raise RequestNotSent('cover_stopped')
        if self.clock() - context['_prepared_at'] > self.ttl:
            raise RequestNotSent('cover_context_expired')

    def _finish(self, task, status, result):
        # Successful binding and its cleanup enqueue commit atomically.
        self.store.finish_task(task['id'], status, result)
        return {'task_id': task['id'], 'status': status, 'result': result}

    def _defer(self, task, result):
        self.store.defer_task(task['id'], result)
        return {'task_id': task['id'], 'status': 'pending', 'result': result}

    def _resource(self, graph, task, context, payload):
        scope = str(context.get('cover_version') or ('batch:' + task['batch_id']))
        key = hashlib.sha256((payload_key(task['account_id'], payload) + ':' + scope).encode()).hexdigest()
        with self.mutex:
            lock = self.resource_locks.setdefault(key, threading.Lock())
        # The per-key mutex prevents identical workers from issuing duplicate
        # POSTs. The SQLite claim also protects restart/process boundaries.
        with lock:
            self._before(context)
            row = self.store.claim_resource(key, task['account_id'], task['batch_id'], payload)
            if not row['claimed']:
                if row['status'] == 'accepted':
                    value = row['result']
                    new_id = str(value.get('new_creative_id') or '')
                    if not new_id:
                        return {'status': 'unknown', 'resource_key': key, 'reason': 'resource_id_missing'}
                    with self.mutex:
                        verified = key in self.verified_resources
                    if not verified:
                        actual = graph.get_creative(new_id, context['user_id'], task['account_id'])
                        verify_creative(actual, payload)
                        with self.mutex:
                            self.verified_resources.add(key)
                    return dict(value, status='accepted', resource_key=key, reused=True)
                if row['status'] in ('failed', 'unknown'):
                    return dict(row['result'], status=row['status'], resource_key=key,
                                reason='creative_dependency_' + row['status'])
                raise RequestNotSent('cover_resource_busy')

            def before_create():
                self._before(context)
                self.store.mark_resource_write(key)

            try:
                result = graph.create_creative(task['account_id'], context['user_id'], payload, before_create)
            except RequestNotSent as exc:
                self.store.defer_resource(key, {'reason': exc.code, 'request_sent': False})
                raise
            except Exception as exc:
                # Callback/ledger exceptions must stop all new mutations. A
                # durable intent may already exist, so preserve uncertainty.
                self._halt('creative_write_unhandled')
                saved = self.store.get_resource(key)
                status = 'unknown' if saved.get('request_sent') else 'failed'
                result = {'status': status, 'error': _error(exc),
                          'request_sent': bool(saved.get('request_sent'))}
                self.store.finish_resource(key, status, result)
                raise
            if not result.get('request_sent') and _reason(result) in _DEFER_CODES:
                if self.store.get_resource(key).get('request_sent'):
                    result = dict(result, status='unknown', reason='create_intent_interrupted')
                else:
                    self.store.defer_resource(key, result)
                    raise RequestNotSent(_reason(result))
            if result['status'] == 'accepted':
                new_id = str(result.get('new_creative_id') or '')
                try:
                    actual = graph.get_creative(new_id, context['user_id'], task['account_id'])
                    verify_creative(actual, payload)
                    result = dict(result, verified=True, creative_snapshot=actual)
                    with self.mutex:
                        self.verified_resources.add(key)
                except PayloadError as exc:
                    result = dict(result, status='unknown', error=_error(exc), reason='creative_parameters_changed')
                    self._halt('creative_parameters_changed')
                except Exception as exc:
                    result = dict(result, status='unknown', error=_error(exc), reason='creative_readback_unavailable')
            self.store.finish_resource(key, result['status'], result)
            return dict(result, resource_key=key)

    def _execute(self, task, context):
        if not self.store.claim_task(task['id']):
            return {'task_id': task['id'], 'status': 'not_claimed'}
        owner = 'cover-task:' + str(task['id'])
        reserved = False
        uncertain_bind = False
        graph = self._graph()
        result = {}
        try:
            self._before(context)
            if context.get('validated') is not True:
                return self._finish(task, 'failed', {'reason': 'source_validation_failed',
                    'error': context.get('error') or {}, 'request_sent': False})
            for field in ('account_id', 'ad_id', 'old_creative_id'):
                if str(context.get(field)) != str(task[field]):
                    return self._finish(task, 'obsolete', {'reason': 'source_binding_changed', 'request_sent': False})
            user = str(context['user_id'])
            if str(context.get('root_id')) not in ('1479', '3302') or not context.get('material_ids'):
                return self._finish(task, 'failed', {'reason': 'source_scope_unverified', 'request_sent': False})
            if self.store.is_cover_creative(task['account_id'], task['old_creative_id']):
                return self._finish(task, 'obsolete', {'reason': 'cover_already_applied', 'request_sent': False})
            ad = graph.get_cover_ad(task['ad_id'], user, task['account_id'])
            state, reason = review_state(ad)
            if state != 'ready':
                return self._finish(task, state, {'reason': reason, 'ad_snapshot': ad, 'request_sent': False})
            if _binding(ad) != task['old_creative_id']:
                return self._finish(task, 'obsolete', {'reason': 'creative_changed', 'ad_snapshot': ad, 'request_sent': False})
            self._before(context)
            reservation = self.shared.reserve_cover_resources(owner, task['account_id'],
                ad_id=task['ad_id'], creative_ids=(task['old_creative_id'],))
            if not reservation['claimed']:
                reason = reservation.get('reason', 'shared_resource_busy')
                result = {'reason': reason, 'shared_proof': reservation.get('proof') or {}, 'request_sent': False}
                if reason == 'previous_deleted':
                    return self._finish(task, 'obsolete', result)
                if 'unknown' in reason or reason.startswith('cover_delete_once_'):
                    return self._finish(task, 'blocked', result)
                return self._defer(task, result)
            reserved = True
            old = graph.get_creative(task['old_creative_id'], user, task['account_id'])
            payload = build_payload(old, context['cover_url'])
            if not payload.get('name'):
                payload['name'] = 'cover-' + payload_key(task['account_id'], payload)[:24]
            resource = self._resource(graph, task, context, payload)
            if resource['status'] != 'accepted':
                return self._finish(task, 'unknown' if resource['status'] == 'unknown' else 'failed',
                    {'reason': 'creative_dependency_' + resource['status'], 'resource': resource,
                     'request_sent': False, 'user_id': user})
            new_id = str(resource['new_creative_id'])
            if new_id == task['old_creative_id']:
                self._halt('replacement_creative_unchanged')
                return self._finish(task, 'failed', {'reason': 'replacement_creative_unchanged', 'request_sent': False})
            self._before(context)
            reservation = self.shared.reserve_cover_resources(owner, task['account_id'],
                ad_id=task['ad_id'], creative_ids=(task['old_creative_id'], new_id))
            if not reservation['claimed']:
                reason = reservation.get('reason', 'shared_resource_busy')
                result = {'reason': reason, 'request_sent': False, 'new_creative_id': new_id}
                if reason == 'previous_deleted' or 'unknown' in reason or reason.startswith('cover_delete_once_'):
                    return self._finish(task, 'blocked', result)
                return self._defer(task, result)
            fresh = graph.get_cover_ad(task['ad_id'], user, task['account_id'])
            state, reason = review_state(fresh)
            if state != 'ready':
                return self._finish(task, state, {'reason': reason, 'ad_snapshot': fresh, 'request_sent': False})
            if _binding(fresh) != task['old_creative_id']:
                return self._finish(task, 'obsolete', {'reason': 'creative_changed_before_bind', 'request_sent': False})
            # Detect an operator changing business parameters while the image
            # was prepared, before this worker writes anything to the Ad.
            try:
                verify_ad_binding(fresh, ad, task['old_creative_id'])
            except PayloadError:
                return self._finish(task, 'obsolete', {'reason': 'ad_parameters_changed_before_bind', 'request_sent': False})
            intent = {'new_creative_id': new_id, 'expected_ad': fresh,
                      'old_creative_snapshot': old,
                      'resource_key': resource['resource_key'], 'user_id': user,
                      'cover_url': context['cover_url']}

            def before_bind():
                self._before(context)
                self.store.mark_task_write(task['id'], 'binding', intent=intent)

            result = graph.bind_creative(task['ad_id'], new_id, user, task['account_id'], before_bind)
            uncertain_bind = bool(result.get('request_sent')) and result['status'] == 'unknown'
            if not result.get('request_sent') and _reason(result) in _DEFER_CODES:
                if self.store.get_task(task['id']).get('request_sent'):
                    uncertain_bind = True
                    return self._finish(task, 'unknown', dict(intent, reason='binding_intent_interrupted',
                                                               bind_result=result, request_sent=False))
                return self._defer(task, dict(intent, reason=_reason(result), request_sent=False))
            if result['status'] == 'failed':
                return self._finish(task, 'failed', dict(intent, bind_result=result,
                                                        reason='bind_failed', request_sent=result.get('request_sent', False)))
            try:
                actual = graph.get_cover_ad(task['ad_id'], user, task['account_id'])
                if _binding(actual) != new_id and result['status'] == 'unknown':
                    return self._finish(task, 'unknown', dict(intent, bind_result=result,
                        reason='binding_result_unknown', request_sent=True, ad_snapshot=actual))
                verify_ad_binding(actual, fresh, new_id)
            except PayloadError as exc:
                uncertain_bind = True
                self._halt('ad_parameters_changed_after_bind')
                return self._finish(task, 'unknown', dict(intent, bind_result=result,
                    reason='ad_parameters_changed_after_bind', request_sent=True, error=_error(exc)))
            except Exception as exc:
                uncertain_bind = True
                return self._finish(task, 'unknown', dict(intent, bind_result=result,
                    reason='binding_readback_unavailable', request_sent=True, error=_error(exc)))
            uncertain_bind = False
            return self._finish(task, 'succeeded', dict(intent, bind_result=result,
                confirmed_binding=True, request_sent=True, ad_snapshot=actual))
        except RequestNotSent as exc:
            saved = self.store.get_task(task['id'])
            if saved['request_sent']:
                uncertain_bind = True
                return self._finish(task, 'unknown', dict(saved.get('result') or {}, reason=exc.code, request_sent=True))
            return self._defer(task, {'reason': exc.code, 'request_sent': False})
        except (PayloadError, GraphError) as exc:
            saved = self.store.get_task(task['id'])
            uncertain_bind = bool(saved['request_sent'])
            return self._finish(task, 'unknown' if uncertain_bind else 'failed',
                dict(saved.get('result') or {}, reason='preflight_or_graph_failed',
                     error=_error(exc), request_sent=uncertain_bind))
        except Exception as exc:
            self._halt('worker_or_ledger_error')
            saved = self.store.get_task(task['id'])
            uncertain_bind = bool(saved['request_sent'])
            return self._finish(task, 'unknown' if uncertain_bind else 'failed',
                dict(saved.get('result') or {}, reason='worker_or_ledger_error',
                     error=_error(exc), request_sent=uncertain_bind))
        finally:
            if reserved:
                try:
                    self.shared.release_cover_resources(owner, unknown=uncertain_bind)
                except Exception:
                    self._halt('shared_reservation_release_failed')
                    raise

    def run_once(self, max_ads=256, process_deletes=True):
        if self.config.get('cover_live_enabled') is not True:
            raise ValueError('cover_live_disabled')
        if not isinstance(max_ads, int) or not 1 <= max_ads <= 100000:
            raise ValueError('invalid_max_ads')
        started = self.clock()
        tasks = list(_fair(self.store.ready_tasks(limit=max_ads * 4)))[:max_ads]
        outputs = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for offset in range(0, len(tasks), self.batch_size):
                if self.stopped():
                    break
                wave = tasks[offset:offset + self.batch_size]
                # This is deliberately before thread submission: no worker may
                # consume a FIFO SQL session or mix source snapshots.
                prepared = self.source.prepare_candidates(wave)
                at = self.clock()
                futures = {}
                for task in wave:
                    context = deepcopy(prepared.get(str(task['id'])) or
                                       {'validated': False, 'error': {'code': 'prepared_context_missing'}})
                    context['_prepared_at'] = at
                    futures[pool.submit(self._execute, task, context)] = task
                for future in as_completed(futures):
                    try:
                        outputs.append(future.result())
                    except Exception:
                        self._halt('worker_future_failed')
                        raise
        deletion = self.process_deletions() if process_deletes and not self.stopped() else []
        governors = {id(g.governor): g.governor for g in self.clients if hasattr(g, 'governor')}
        metrics = [g.snapshot() for g in governors.values()]
        return {'tasks': dict(Counter(item['status'] for item in outputs)),
                'deletions': dict(Counter(item['status'] for item in deletion)),
                'task_results': outputs, 'deletion_results': deletion,
                'governor': metrics[0] if len(metrics) == 1 else metrics,
                'seconds': round(self.clock() - started, 3),
                'halted': self.halted.is_set(), 'halt_reason': self.halt_reason}

    def _scan_references(self, group):
        """Batch known blockers, then read one complete inventory per account."""
        account, user = group[0]['account_id'], group[0]['user_id']
        graph = self._graph()
        outputs = []
        known_ids = list(dict.fromkeys(aid for row in group
            for aid in (row.get('result', {}).get('blocker_ad_ids') or [])))
        records, errors = {}, {}
        for start in range(0, len(known_ids), 50):
            identifiers = known_ids[start:start + 50]
            try:
                found, failed = graph.get_reference_ads(identifiers, user, account)
                records.update(found)
                errors.update(failed)
                for aid in identifiers:
                    if aid not in found:
                        errors.setdefault(aid, {'code': 'reference_missing'})
            except (GraphError, RequestNotSent) as exc:
                errors.update({aid: _error(exc) for aid in identifiers})
        eligible = []
        for row in group:
            known = row.get('result', {}).get('blocker_ad_ids') or []
            unresolved = [aid for aid in known if aid in errors]
            incomplete = bool(unresolved)
            blockers = [aid for aid in known if aid in records
                and 'DELETED' not in (records[aid].get('status'), records[aid].get('effective_status'))
                and _binding(records[aid]) == row['creative_id']]
            if blockers or incomplete:
                result = {'complete': not incomplete,
                          'blocker_ad_ids': list(dict.fromkeys(blockers + unresolved)),
                          'unresolved_ad_ids': unresolved,
                          'reason': 'reference_scan_incomplete' if incomplete else 'creative_still_referenced'}
                self.store.set_delete_waiting(account, row['creative_id'], result)
                outputs.append(dict(result, creative_id=row['creative_id'], status=
                    'reference_scan_incomplete' if incomplete else 'waiting_references'))
            else:
                eligible.append(row)
        if not eligible or self.stopped():
            return [], outputs
        ready = []
        try:
            inventory = graph.list_reference_ads(account, user)
            by_creative = defaultdict(list)
            for ad in inventory:
                if str(ad.get('account_id')) != account or not _binding(ad):
                    raise GraphError('incomplete_reference_identity', 'Reference identity incomplete')
                if 'DELETED' not in (ad.get('status'), ad.get('effective_status')):
                    by_creative[_binding(ad)].append(str(ad['id']))
            checked = self.clock()
            for row in eligible:
                blockers = by_creative.get(row['creative_id'], [])
                if blockers:
                    result = {'complete': True, 'blocker_ad_ids': blockers, 'reason': 'creative_still_referenced'}
                    self.store.set_delete_waiting(account, row['creative_id'], result)
                    outputs.append(dict(result, creative_id=row['creative_id'], status='waiting_references'))
                else:
                    ready.append((row, checked))
        except (GraphError, RequestNotSent) as exc:
            for row in eligible:
                result = {'complete': False, 'reason': 'reference_scan_incomplete', 'error': _error(exc)}
                self.store.set_delete_waiting(account, row['creative_id'], result)
                outputs.append(dict(result, creative_id=row['creative_id'], status='reference_scan_incomplete'))
        return ready, outputs

    def process_deletions(self, limit=1000):
        """Drain Ad workers first; share the same bounded pool for reads/deletes."""
        groups = defaultdict(list)
        for row in self.store.pending_deletions(limit=limit):
            groups[row['account_id']].append(row)
        remaining, ready, outputs = deque(groups.values()), deque(), []
        # Scheduling deletes as each inventory finishes avoids aging every
        # account snapshot while unrelated accounts are still being paged.
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {}
            while (remaining or ready or futures) and not self.stopped():
                scans = sum(kind == 'scan' for kind in futures.values())
                while len(futures) < self.workers:
                    if ready:
                        row, checked = ready.popleft()
                        futures[pool.submit(self._delete_one, row, checked)] = 'delete'
                    elif remaining and scans < min(4, self.workers):
                        futures[pool.submit(self._scan_references, remaining.popleft())] = 'scan'
                        scans += 1
                    else:
                        break
                if not futures:
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    kind = futures.pop(future)
                    try:
                        value = future.result()
                    except Exception:
                        self._halt('delete_worker_or_ledger_error')
                        raise
                    if kind == 'scan':
                        ready.extend(value[0]); outputs.extend(value[1])
                    else:
                        outputs.append(value)
            # Exiting the executor drains in-flight requests even on stop. Their
            # durable outcomes still contribute to this wave's report.
            for future, kind in futures.items():
                value = future.result()
                if kind == 'scan':
                    outputs.extend(value[1])
                else:
                    outputs.append(value)
        return outputs

    def _import_delete_fence(self, row, claim):
        """Reuse a receipt or consumed shared attempt without another DELETE."""
        account, creative = row['account_id'], row['creative_id']
        owner = 'cover-delete:' + account + ':' + creative
        guard = self.shared.get_cover_delete_once(account, creative)
        reason = str(claim.get('reason') or '')
        status = str(claim.get('status') or '')
        if reason == 'previous_deleted' or status in ('deleted_verified', 'previous_deleted'):
            status = 'previous_deleted'
        elif guard:
            status = guard['status']
        elif 'unknown' in reason or status == 'unknown':
            status = 'unknown'
        else:
            return None
        if status == 'in_progress':
            status = 'unknown'
        if status not in ('deleted_verified', 'previous_deleted', 'failed', 'blocked', 'unknown'):
            return None
        proof = {'reason': 'shared_delete_receipt' if status in ('deleted_verified', 'previous_deleted')
                 else 'shared_delete_attempt_consumed', 'shared_guard': guard,
                 'shared_result': claim.get('proof') or {}, 'request_sent': False,
                 'shared_delete_claimed': bool(guard and guard['owner'] == owner)}
        if status in ('deleted_verified', 'previous_deleted'):
            proof['confirmed_deleted'] = True
        self.store.finish_delete(account, creative, status, proof)
        return {'creative_id': creative, 'status': status, 'result': proof}

    @staticmethod
    def _read_deleted(graph, creative, user, account):
        # Minimal identity/status fields remain useful after deletion, while
        # the full Creative payload may no longer be readable. Inaccessible
        # object errors never constitute deletion proof.
        return graph.reconcile('creative', creative, user, account)

    def _delete_one(self, row, checked_at):
        account, creative, user = row['account_id'], row['creative_id'], row['user_id']
        owner = 'cover-delete:' + account + ':' + creative
        graph = self._graph()
        reserved = False
        sent = False
        shared_claimed = False
        fenced = None
        try:
            if self.stopped() or self.clock() - checked_at > self.ttl:
                self.store.set_delete_waiting(account, creative, {'complete': False, 'reason': 'reference_snapshot_expired'})
                return {'creative_id': creative, 'status': 'reference_scan_incomplete'}
            held = self.shared.reserve_cover_resources(owner, account, creative_ids=(creative,))
            if not held['claimed']:
                imported = self._import_delete_fence(row, held)
                if imported:
                    return imported
                self.store.set_delete_waiting(account, creative, {'complete': False, 'reason': held.get('reason', 'shared_resource_busy')})
                return {'creative_id': creative, 'status': 'reference_scan_incomplete'}
            reserved = True

            def before_delete():
                nonlocal sent, shared_claimed, fenced
                if self.stopped():
                    raise RequestNotSent('cover_stopped')
                if self.clock() - checked_at > self.ttl:
                    raise RequestNotSent('cover_context_expired')
                claim = self.shared.claim_cover_delete_once(owner, account, creative)
                if not claim['claimed']:
                    fenced = self._import_delete_fence(row, claim)
                    raise RequestNotSent('cover_delete_shared_fenced')
                shared_claimed = True
                if not self.store.claim_delete_once(account, creative):
                    raise CoverStoreError('cover_delete_already_attempted')
                sent = True

            result = graph.delete_creative(creative, user, account, before_delete)
            if fenced:
                return fenced
            if result['status'] == 'already_deleted':
                proof = dict(result, confirmed_deleted=True)
                self.store.finish_delete(account, creative, 'previous_deleted', proof)
                return {'creative_id': creative, 'status': 'previous_deleted'}
            if not result.get('request_sent'):
                if shared_claimed or sent:
                    result = dict(result, status='unknown', reason='delete_intent_without_response')
                else:
                    self.store.set_delete_waiting(account, creative, {'complete': False, 'reason': _reason(result) or 'delete_not_sent'})
                    return {'creative_id': creative, 'status': 'reference_scan_incomplete'}
            if result['status'] == 'failed':
                status = 'failed'
            elif result['status'] == 'accepted' and (result.get('response') or {}).get('success') is True:
                # CoverGraph accepts only an HTTP 2xx success:true receipt
                # after verifying the exact Creative/account identity.
                status = 'deleted_verified'
                result = dict(result, confirmed_deleted=True, account_id=account,
                              creative_id=creative, proof_source='delete_api_receipt')
            else:
                status = 'unknown'
                try:
                    readback = self._read_deleted(graph, creative, user, account)
                    if readback['status'] == 'deleted_verified':
                        status = 'deleted_verified'
                        result = dict(result, confirmed_deleted=True, object=readback['object'],
                                      proof_source='deleted_status_readback')
                    else:
                        result = dict(result, readback=readback)
                except GraphError as exc:
                    result = dict(result, reconcile_error=_error(exc))
            result = dict(result, shared_delete_claimed=shared_claimed)
            self.store.finish_delete(account, creative, status, result)
            if shared_claimed:
                self.shared.finish_cover_delete(owner, account, creative, status, result)
            return {'creative_id': creative, 'status': status, 'result': result}
        except RequestNotSent as exc:
            if fenced:
                return fenced
            if sent or shared_claimed:
                result = {'reason': exc.code, 'request_sent': sent, 'shared_delete_claimed': shared_claimed}
                self.store.finish_delete(account, creative, 'unknown', result)
                if shared_claimed:
                    self.shared.finish_cover_delete(owner, account, creative, 'unknown', result)
                return {'creative_id': creative, 'status': 'unknown'}
            self.store.set_delete_waiting(account, creative, {'complete': False, 'reason': exc.code})
            return {'creative_id': creative, 'status': 'reference_scan_incomplete'}
        except GraphError as exc:
            # A pre-delete identity GET failure is local to this Creative.
            # No intent was consumed, so an hourly read can check it again.
            if not sent and not shared_claimed:
                result = {'complete': False, 'reason': 'delete_identity_unverified', 'error': _error(exc)}
                self.store.set_delete_waiting(account, creative, result)
                return {'creative_id': creative, 'status': 'reference_scan_incomplete', 'result': result}
            result = {'reason': 'delete_result_unverified', 'error': _error(exc), 'request_sent': sent,
                      'shared_delete_claimed': shared_claimed}
            self.store.finish_delete(account, creative, 'unknown', result)
            if shared_claimed:
                self.shared.finish_cover_delete(owner, account, creative, 'unknown', result)
            return {'creative_id': creative, 'status': 'unknown', 'result': result}
        except Exception as exc:
            self._halt('delete_worker_or_ledger_error')
            result = {'reason': 'delete_worker_or_ledger_error', 'error': _error(exc), 'request_sent': sent,
                      'shared_delete_claimed': shared_claimed}
            self.store.finish_delete(account, creative, 'unknown' if sent or shared_claimed else 'failed', result)
            if shared_claimed:
                self.shared.finish_cover_delete(owner, account, creative, 'unknown', result)
            return {'creative_id': creative, 'status': 'unknown' if sent or shared_claimed else 'failed'}
        finally:
            if reserved:
                saved = self.store.get_delete(account, creative)
                self.shared.release_cover_resources(owner, unknown=saved['status'] == 'unknown')

    def reconcile_unknown(self, limit=1000):
        """Read-only reconciliation never sends a create, bind or delete."""
        graph = self._graph()
        counts = Counter()
        tasks = self.store.unknown_tasks(limit=limit)
        dependencies = {}
        for task in tasks:
            key = (task['result'].get('resource') or {}).get('resource_key')
            if key and not task.get('request_sent'):
                dependencies.setdefault(key, task)
        for resource in self.store.unknown_resources(limit=limit):
            if self.stopped():
                break
            saved = resource['result']
            new_id = saved.get('new_creative_id')
            task = dependencies.get(resource['key'])
            if not new_id or not task:
                counts['unresolved_resources'] += 1
                continue
            try:
                user = str(task['result'].get('user_id') or task['candidate']['user_id'])
                actual = graph.get_creative(str(new_id), user, resource['account_id'])
                verify_creative(actual, resource['payload'])
                proof = dict(saved, verified=True, new_creative_id=str(new_id),
                             readback=actual, creative_snapshot=actual, reconciled=True)
                resumed = self.store.resolve_resource(resource['key'], proof)
                counts['resources_confirmed'] += 1
                counts['tasks_resumed'] += resumed
            except PayloadError:
                self._halt('creative_parameters_changed')
                counts['unresolved_resources'] += 1
            except GraphError:
                counts['unresolved_resources'] += 1
        for task in self.store.unknown_tasks(limit=limit):
            if self.stopped():
                break
            result = task['result']
            new_id, expected = result.get('new_creative_id'), result.get('expected_ad')
            if not task.get('request_sent') or not new_id or not expected:
                counts['unresolved_tasks'] += 1
                continue
            try:
                user = str(result.get('user_id') or task['candidate']['user_id'])
                actual = graph.get_cover_ad(task['ad_id'], user, task['account_id'])
                verify_ad_binding(actual, expected, str(new_id))
                proof = dict(result, confirmed_binding=True, ad_snapshot=actual, reconciled=True)
                self.store.finish_task(task['id'], 'succeeded', proof)
                self.shared.release_cover_resources('cover-task:' + str(task['id']), verified=True)
                counts['tasks_confirmed'] += 1
            except (GraphError, PayloadError):
                counts['unresolved_tasks'] += 1
        for row in self.store.unknown_deletions(limit=limit):
            if self.stopped():
                break
            account, creative = row['account_id'], row['creative_id']
            try:
                readback = self._read_deleted(graph, creative, row['user_id'], account)
                if readback['status'] != 'deleted_verified':
                    counts['unresolved_deletions'] += 1
                    continue
                proof = dict(row['result'], confirmed_deleted=True, object=readback['object'], reconciled=True)
                self.store.finish_delete(account, creative, 'deleted_verified', proof)
                owner = 'cover-delete:' + account + ':' + creative
                guard = self.shared.get_cover_delete_once(account, creative)
                if guard and guard['owner'] == owner and guard['status'] in ('in_progress', 'unknown'):
                    self.shared.finish_cover_delete(owner, account, creative, 'deleted_verified', proof)
                self.shared.release_cover_resources(owner, verified=True)
                counts['deletions_confirmed'] += 1
            except GraphError:
                counts['unresolved_deletions'] += 1
        return dict(counts)
