"""One-off exact-drama runs with mandatory audience and theme matching."""
import hashlib
import re
from types import SimpleNamespace

from features.x_auto_posts.service import XAutoPostService, AutoPostServiceError, _actor, _blacklist_dict
from features.x_auto_posts.repositories import BEIJING_TZ
from .policy import Decision, catalog_labels, language, portrait_snapshot
from features.hit_drama_target import identifier, resolve_target, content_ids

MARKER = 'hit_drama_publish'


def normalize(payload):
    if set(payload) != {'expected_version', 'drama_id', 'id_type', 'idempotency_key', 'confirmed'} or payload.get('confirmed') is not True:
        raise AutoPostServiceError('invalid_request', '爆款剧发布请求无效', 400)
    try:
        value = identifier(payload['id_type'], payload['drama_id'])
    except ValueError as exc:
        raise AutoPostServiceError('invalid_request', str(exc), 400) from None
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,70}', str(payload['idempotency_key'])):
        raise AutoPostServiceError('invalid_request', '操作标识无效', 400)
    if isinstance(payload['expected_version'], bool) or not re.fullmatch(r'[1-9][0-9]{0,8}', str(payload['expected_version'])):
        raise AutoPostServiceError('invalid_request', '模板版本无效', 400)
    return dict(payload, drama_id=value, expected_version=int(payload['expected_version']))


def resolve(source, request):
    try:
        return resolve_target(source._stream_select, source.schema, source.app_id, request)
    except ValueError as exc:
        raise AutoPostServiceError('hit_drama_not_found', str(exc), 409) from None


def match_reason(decision, metadata):
    if decision.reason:
        return decision.reason
    if not metadata:
        return 'candidate_metadata_missing'
    audience = metadata.get('audience')
    if audience not in decision.weights:
        return 'audience_not_matched'
    themes = decision.node_weights.get((audience,), {})
    if not any(themes.get(theme, 0) > 0 for theme in metadata.get('themes', [])):
        return 'theme_not_matched'
    return ''


class TagAutoPostService(XAutoPostService):
    def run_now(self, template_id, raw):
        if 'material_ids' in raw or 'material_id' in raw:
            from .hit_material import publish_material
            return publish_material(self, template_id, raw)
        if 'drama_id' not in raw:
            return super().run_now(template_id, raw)
        payload = dict(raw)
        actor = _actor(payload)
        request = normalize(payload)
        key = 'x-tag:hit-drama:v1:' + hashlib.sha256(request['idempotency_key'].encode()).hexdigest()[:32]
        with self._run_create_lock:
            existing = self.store.get_run_by_key(key)
            if existing:
                frozen = existing.blacklist_snapshot.get(MARKER, {})
                if existing.template_id != int(template_id) or frozen.get('request') != request:
                    raise AutoPostServiceError('hit_drama_operation_conflict', '同一操作标识已用于不同剧目或模板', 409)
                template = self.store.get_template(template_id, version=existing.template_version)
                self._run_tasks(existing, dict(template.config, account_ids=frozen['account_ids']))
                run, created = existing, False
            else:
                if not self.executor.gates.is_open:
                    raise AutoPostServiceError('x_auto_live_gates_closed', 'X 自动发布总开关关闭', 409)
                template = self.store.get_template(template_id)
                if template.version != request['expected_version']:
                    raise AutoPostServiceError('x_auto_template_version_conflict', '模板版本已变化，请刷新后重试', 409)
                target = resolve(self.selector.source, request)
                catalog = catalog_labels(self.selector.source, [SimpleNamespace(content_id=cid) for cid in content_ids(target)])
                accounts, skipped = [], []
                for account_id in template.config.get('account_ids', []):
                    try:
                        account = self._account_snapshot(account_id)
                        if not account.get('publish_eligible'):
                            raise ValueError('account_not_publishable')
                        accounts.append(account)
                    except Exception:
                        skipped.append(dict(account_id=account_id, reason='account_not_publishable'))
                snapshot = portrait_snapshot([a.get('x_user_id') for a in accounts])
                matched, audits, account_targets = [], {}, {}
                for account in accounts:
                    account_id = str(account['id'])
                    lang = self._account_language(account)
                    decision = Decision(account, ['hit-drama', key], snapshot)
                    ids = content_ids(target, lang)
                    eligible = [cid for cid in ids if not match_reason(decision, catalog.get((cid, language(lang))))]
                    reason = '' if eligible else ('language_not_matched' if not ids else match_reason(decision, catalog.get((ids[0], language(lang)))))
                    if reason:
                        skipped.append(dict(account_id=account_id, reason=reason))
                    else:
                        matched.append(account_id)
                        account_targets[account_id] = eligible
                        audits[account_id] = decision.audit(catalog[(eligible[0], language(lang))])
                if not matched:
                    raise AutoPostServiceError('hit_drama_no_matching_channel', '没有语言、受众和题材画像均匹配的可发布频道，未创建任务', 409)
                from features.hit_publish import release_times, POLICY
                times = release_times(self.selector.source._stream_select, self.selector.source.schema, self.selector.source.app_id, content_ids(target))
                frozen = dict(release_times=times, timing_policy=POLICY, request=request, target=target, account_ids=matched, account_content_ids=account_targets, skipped=skipped, profiles=audits)
                now = self._now()
                run = self.store.create_run(run_key=key, template_id=template.id, template_version=template.version,
                    trigger_type='manual', scheduled_at_utc=now, shanghai_date=now.astimezone(BEIJING_TZ).date().isoformat(),
                    publish_time=now.astimezone(BEIJING_TZ).strftime('%H:%M'), actor=actor,
                    blacklist_snapshot={**_blacklist_dict(self.selector.source.blacklist_snapshot()), MARKER: frozen})
                self._run_tasks(run, dict(template.config, account_ids=matched))
                created = True
        self._kick()
        return dict(ok=True, run_id=run.id, created=created, idempotent=not created,
                    matched_channels=len(frozen['account_ids']), skipped_channels=frozen['skipped'], content_id=frozen['target'].get('content_id',''),
                    content_ids=content_ids(frozen['target']), languages=frozen['target'].get('languages',[]))
