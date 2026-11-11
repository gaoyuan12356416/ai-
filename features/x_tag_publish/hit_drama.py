"""One-off exact-drama runs with mandatory audience and theme matching."""
import hashlib
import re
from types import SimpleNamespace

from features.x_auto_posts.service import XAutoPostService, AutoPostServiceError, _actor, _blacklist_dict
from features.x_auto_posts.repositories import BEIJING_TZ
from .policy import Decision, catalog_labels, language, portrait_snapshot

MARKER = 'hit_drama_publish'


def normalize(payload):
    if set(payload) != {'expected_version', 'drama_id', 'id_type', 'idempotency_key', 'confirmed'} or payload.get('confirmed') is not True:
        raise AutoPostServiceError('invalid_request', '爆款剧发布请求无效', 400)
    if payload['id_type'] not in ('content_id', 'resource_id') or not re.fullmatch(r'[1-9][0-9]{0,30}', str(payload['drama_id'])):
        raise AutoPostServiceError('invalid_request', '请输入一个有效的剧 ID 或资源 ID', 400)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,70}', str(payload['idempotency_key'])):
        raise AutoPostServiceError('invalid_request', '操作标识无效', 400)
    if isinstance(payload['expected_version'], bool) or not re.fullmatch(r'[1-9][0-9]{0,8}', str(payload['expected_version'])):
        raise AutoPostServiceError('invalid_request', '模板版本无效', 400)
    return dict(payload, drama_id=str(payload['drama_id']), expected_version=int(payload['expected_version']))


def resolve(source, request):
    if request['id_type'] == 'resource_id':
        rows = list(source._stream_select(f'SELECT DISTINCT content_id FROM `{source.schema}`.ads_drama_resource WHERE id=%s AND app_id=%s LIMIT 2', (request['drama_id'], source.app_id)))
        ids = {str(r['content_id']).strip() for r in rows if r.get('content_id')}
        if len(ids) != 1:
            raise AutoPostServiceError('hit_drama_not_found', '资源 ID 未关联到目标产品下的唯一剧目', 409)
        content_id = ids.pop()
    else:
        content_id = request['drama_id']
    rows = list(source._stream_select(f'SELECT DISTINCT content_id,language FROM `{source.schema}`.ads_drama_info WHERE app_id=%s AND content_id=%s LIMIT 100', (source.app_id, content_id)))
    if not rows or len(rows) == 100:
        raise AutoPostServiceError('hit_drama_not_found', '未找到目标产品下的唯一剧目资料', 409)
    return {'content_id': content_id, 'languages': sorted({language(r['language']) for r in rows if r.get('language')})}


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
                catalog = catalog_labels(self.selector.source, [SimpleNamespace(content_id=target['content_id'])])
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
                matched, audits = [], {}
                for account in accounts:
                    account_id = str(account['id'])
                    lang = self._account_language(account)
                    decision = Decision(account, ['hit-drama', key], snapshot)
                    metadata = catalog.get((target['content_id'], language(lang)))
                    reason = 'language_not_matched' if lang not in target['languages'] else match_reason(decision, metadata)
                    if reason:
                        skipped.append(dict(account_id=account_id, reason=reason))
                    else:
                        matched.append(account_id)
                        audits[account_id] = decision.audit(metadata)
                if not matched:
                    raise AutoPostServiceError('hit_drama_no_matching_channel', '没有语言、受众和题材画像均匹配的可发布频道，未创建任务', 409)
                frozen = dict(request=request, target=target, account_ids=matched, skipped=skipped, profiles=audits)
                now = self._now()
                run = self.store.create_run(run_key=key, template_id=template.id, template_version=template.version,
                    trigger_type='manual', scheduled_at_utc=now, shanghai_date=now.astimezone(BEIJING_TZ).date().isoformat(),
                    publish_time=now.astimezone(BEIJING_TZ).strftime('%H:%M'), actor=actor,
                    blacklist_snapshot={**_blacklist_dict(self.selector.source.blacklist_snapshot()), MARKER: frozen})
                self._run_tasks(run, dict(template.config, account_ids=matched))
                created = True
        self._kick()
        return dict(ok=True, run_id=run.id, created=created, idempotent=not created,
                    matched_channels=len(frozen['account_ids']), skipped_channels=frozen['skipped'], content_id=frozen['target']['content_id'])
