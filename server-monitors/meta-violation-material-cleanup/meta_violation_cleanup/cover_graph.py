"""Budgeted Meta transport for cover replacement and one-shot Creative deletion.

One instance belongs to one worker thread; all workers share one Governor.
Every mutation requires a durable before_send callback after its slot is held.
Transport-level retries are disabled and writes are never replayed here.
"""
import json
import requests

from .cover_payload import TOP_PRESERVE, TOP_UNSUPPORTED
from .graph import GraphClient, GraphError, _object_id, CURRENT_STATUSES
from .throttle import RequestNotSent


COVER_AD_FIELDS = (
    'id,account_id,name,status,configured_status,effective_status,issues_info,'
    'ad_review_feedback,creative{id},adset{id,status},campaign{id,status},'
    'adset_id,campaign_id,tracking_specs,conversion_specs,bid_amount'
)
# The SDK v25.0.0 exposes instagram_user_id at Creative level. The older
# instagram_actor_id alias is preserved if inside an object_story_spec response,
# but is not requested as a top-level field that could invalidate the whole GET.
_CREATIVE_TOP_FIELDS = ((TOP_PRESERVE | TOP_UNSUPPORTED) - {'instagram_actor_id', 'instagram_story_id', 'adlabels'})
COVER_CREATIVE_FIELDS = ','.join(sorted(_CREATIVE_TOP_FIELDS | {
    'id', 'account_id', 'name', 'status', 'object_type', 'object_story_spec',
    'image_hash', 'image_url', 'video_id', 'effective_object_story_id',
})) + ',adlabels{id}'
REFERENCE_FIELDS = 'id,account_id,status,effective_status,creative{id}'
REFERENCE_STATUSES = CURRENT_STATUSES + ('ARCHIVED',)


class _BudgetedSession:
    def __init__(self, session, governor, stop):
        self.session, self.governor, self.stop = session, governor, stop
        self.account = ''
        self.before_send = None
        self.request_sent = False

    def request(self, method, *args, **kwargs):
        method = method.upper()
        with self.governor.slot(self.account, self.stop):
            if method in ('POST', 'DELETE'):
                if not callable(self.before_send):
                    raise RequestNotSent('durable_write_guard_required')
                if self.before_send() is False:
                    raise RequestNotSent('write_guard_rejected')
                if self.stop():
                    raise RequestNotSent('cancelled_before_request')
                # Set before the call: an exception from requests cannot prove
                # the remote endpoint did not receive this request.
                self.request_sent = True
            response = self.session.request(method, *args, **kwargs)
            self.governor.observe(response, self.account)
            return response

    def close(self):
        if hasattr(self.session, 'close'):
            self.session.close()


class CoverGraph(GraphClient):
    def __init__(self, tokens, version='v25.0', timeout=20, *, governor,
                 session=None, stop=None, **kwargs):
        if governor is None or not callable(getattr(governor, 'slot', None)):
            raise ValueError('cover_governor_required')
        # Nested GraphClient read pools construct ungoverned clients. Cover
        # orchestration owns concurrency, so its clients always stay serial.
        kwargs.pop('read_workers', None)
        super().__init__(tokens, version, timeout, session=session, read_workers=1, **kwargs)
        self.stop = stop or (lambda: False)
        self.http = _BudgetedSession(self.http, governor, lambda: self.stop())
        self.governor = governor

    def _account(self, account):
        account = _object_id(account, account=True)
        self.http.account = account
        return account

    def _full_record(self, data, identifier, account):
        if not isinstance(data, dict):
            raise GraphError('invalid_object', 'Meta did not return a valid object')
        if _object_id(data.get('id')) != identifier:
            raise GraphError('object_mismatch', 'Meta returned a different object')
        if _object_id(data.get('account_id'), account=True) != account:
            raise GraphError('account_mismatch', 'Meta returned a different account')
        record = self._clean(data)
        record['id'], record['account_id'] = identifier, account
        return record

    def get_creative(self, identifier, user_id, account_id):
        identifier, account = _object_id(identifier), self._account(account_id)
        return self._full_record(self._get(identifier, user_id, {'fields': COVER_CREATIVE_FIELDS}), identifier, account)

    def get_cover_ad(self, identifier, user_id, account_id):
        identifier, account = _object_id(identifier), self._account(account_id)
        record = self._full_record(self._get(identifier, user_id, {'fields': COVER_AD_FIELDS}), identifier, account)
        if record.get('status') != 'DELETED' and record.get('effective_status') != 'DELETED':
            _object_id((record.get('creative') or {}).get('id'))
        return record

    def _object(self, kind, identifier, user_id, account_id):
        identifier, account = _object_id(identifier), self._account(account_id)
        if kind == 'ad':
            return self.get_cover_ad(identifier, user_id, account)
        if kind == 'creative':
            return self._full_record(self._get(identifier, user_id,
                {'fields': 'id,account_id,status'}), identifier, account)
        raise GraphError('invalid_kind', 'Only Ad and Creative objects are supported')

    def _target_ads(self, item, stop=lambda: False):
        self._account(item[1])
        return super()._target_ads(item, stop)

    def get_reference_ads(self, ad_ids, user_id, account_id):
        account, user = self._account(account_id), _object_id(user_id)
        identifiers = list(dict.fromkeys(_object_id(value) for value in ad_ids))
        if not 1 <= len(identifiers) <= 50:
            raise ValueError('reference_batch_requires_1_to_50_ids')
        _, records, errors = self._target_ads((user, account, identifiers), self.stop)
        for identifier, record in list(records.items()):
            if record.get('status') == 'DELETED' or record.get('effective_status') == 'DELETED':
                continue
            try:
                _object_id((record.get('creative') or {}).get('id'))
            except GraphError as exc:
                records.pop(identifier)
                errors[identifier] = exc.as_dict()
        return records, errors

    @staticmethod
    def _form(payload):
        return {key: json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                if isinstance(value, (dict, list, bool)) else value
                for key, value in payload.items()}

    def _write(self, method, path, user_id, account, payload, before_send, *, creates=False):
        self._account(account)
        token = self._token(user_id)
        if not callable(before_send):
            raise ValueError('durable_write_guard_required')
        data = self._form(payload)
        self.http.before_send = before_send
        self.http.request_sent = False
        try:
            response = self.http.request(method, self.base + path, data=data,
                headers={'Authorization': 'Bearer ' + token},
                timeout=self.timeout, allow_redirects=False)
        except RequestNotSent as exc:
            return {'status': 'failed', 'request_sent': False, 'code': exc.code,
                    'error': {'code': exc.code, 'message': 'Request was not sent'}}
        except requests.RequestException:
            sent = self.http.request_sent
            return {'status': 'unknown' if sent else 'failed', 'request_sent': sent,
                    'code': 'transport_uncertain' if sent else 'request_not_sent'}
        finally:
            self.http.before_send = None
        self._observe_usage(response)
        try:
            body = response.json()
        except (TypeError, ValueError):
            body = None
        detail = self._detail(body, response)
        result = {'status': 'unknown', 'request_sent': True, 'response': detail}
        if response.status_code >= 500 or response.status_code == 408 or 300 <= response.status_code < 400:
            return result
        if response.status_code == 429 or isinstance(body, dict) and body.get('error') is not None:
            result.update(status='failed', code=detail.get('code', 'graph_write_failed'),
                          error={'code': detail.get('code', 'graph_write_failed'),
                                 'message': detail.get('message', 'Meta rejected the request'),
                                 'detail': detail})
            return result
        if 200 <= response.status_code < 300:
            if creates and isinstance(body, dict):
                try:
                    created = _object_id(body.get('id'))
                except GraphError:
                    return result
                result.update(status='accepted', new_creative_id=created)
            elif not creates and (body is True or isinstance(body, dict) and body.get('success') is True):
                result.update(status='accepted', response={'http_status': response.status_code, 'success': True})
            elif body is False or isinstance(body, dict) and body.get('success') is False:
                result.update(status='failed', code='platform_rejected')
        return result

    def create_creative(self, account_id, user_id, payload, before_send):
        account = _object_id(account_id, account=True)
        if not isinstance(payload, dict) or not isinstance(payload.get('object_story_spec'), dict):
            raise ValueError('creative_payload_required')
        return self._write('POST', 'act_' + account + '/adcreatives', user_id,
                           account, payload, before_send, creates=True)

    def bind_creative(self, ad_id, creative_id, user_id, account_id, before_send):
        identifier, creative = _object_id(ad_id), _object_id(creative_id)
        # The sole writable Ad field. Never include status, budgets or tracking.
        return self._write('POST', identifier, user_id, account_id,
                           {'creative': {'creative_id': creative}}, before_send)

    def delete_creative(self, creative_id, user_id, account_id, before_send):
        identifier, account = _object_id(creative_id), self._account(account_id)
        record = self._full_record(self._get(identifier, user_id,
            {'fields': 'id,account_id,status'}), identifier, account)
        if record.get('status') == 'DELETED':
            return {'status': 'already_deleted', 'request_sent': False,
                    'confirmed_deleted': True, 'object': record}
        if record.get('status') not in ('ACTIVE', 'IN_PROCESS', 'WITH_ISSUES'):
            raise GraphError('creative_status_unknown', 'Creative status could not be verified')
        return self._write('DELETE', identifier, user_id, account, {}, before_send)

    def list_reference_ads(self, account_id, user_id):
        account = self._account(account_id)
        found = {}
        for rows in self._pages('act_' + account + '/ads', user_id,
                               {'fields': REFERENCE_FIELDS, 'limit': 100,
                                'effective_status': json.dumps(REFERENCE_STATUSES)}):
            for row in rows:
                identifier = _object_id(row.get('id'))
                record = self._full_record(row, identifier, account)
                status, effective = record.get('status'), record.get('effective_status')
                if status == 'DELETED' or effective == 'DELETED':
                    continue
                if status not in ('ACTIVE', 'PAUSED', 'ARCHIVED') or effective not in REFERENCE_STATUSES:
                    raise GraphError('reference_status_unknown', 'Reference status could not be verified')
                _object_id((record.get('creative') or {}).get('id'))
                if identifier in found and found[identifier] != record:
                    raise GraphError('inventory_changed', 'Reference inventory changed during pagination')
                found[identifier] = record
        return list(found.values())
