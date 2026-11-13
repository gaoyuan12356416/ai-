"""Explicit material IDs assigned once to the best eligible profile targets."""
import hashlib
import re
from types import SimpleNamespace

from features.hit_publish import POLICY, assign, future_scope, material_ids, profile_score, release_times, timing
from features.hit_drama_target import target_language
from features.x_auto_posts.service import AutoPostServiceError, _actor, _blacklist_dict
from features.x_auto_posts.publisher import account_duration_limit_seconds
from features.x_auto_posts.repositories import BEIJING_TZ
from .policy import Decision, catalog_labels, language, portrait_snapshot
from .hit_drama import match_reason

MARKER = 'hit_material_publish'


def normalize(raw):
    value = dict(raw)
    actor = _actor(value)
    field = 'material_ids' if 'material_ids' in value else 'material_id'
    if set(value) != {'expected_version', field, 'idempotency_key', 'confirmed'} or value['confirmed'] is not True:
        raise AutoPostServiceError('invalid_request', '爆款素材发布请求无效', 400)
    try:
        ids = material_ids(value[field] if field == 'material_ids' else [value[field]])
    except ValueError as exc:
        raise AutoPostServiceError('invalid_request', str(exc), 400) from None
    if isinstance(value['expected_version'], bool) or not re.fullmatch(r'[1-9][0-9]{0,8}', str(value['expected_version'])):
        raise AutoPostServiceError('invalid_request', '模板版本无效', 400)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,70}', str(value['idempotency_key'])):
        raise AutoPostServiceError('invalid_request', '操作标识无效', 400)
    return actor, dict(expected_version=int(value['expected_version']), material_ids=ids,
                       idempotency_key=value['idempotency_key'], confirmed=True)


def publish_material(service, template_id, raw):
    actor, request = normalize(raw)
    key = 'x-tag:hit-material:v1:'+hashlib.sha256(request['idempotency_key'].encode()).hexdigest()[:32]
    with service._run_create_lock:
        existing = service.store.get_run_by_key(key)
        if existing:
            frozen = existing.blacklist_snapshot.get(MARKER, {})
            if existing.template_id != int(template_id) or frozen.get('request') != request:
                raise AutoPostServiceError('hit_material_operation_conflict', '操作标识已用于其他素材或模板', 409)
            template = service.store.get_template(template_id, version=existing.template_version)
            service._run_tasks(existing, dict(template.config, account_ids=frozen['account_ids']))
            run, created = existing, False
        else:
            if not service.executor.gates.is_open:
                raise AutoPostServiceError('x_auto_live_gates_closed', 'X 自动发布总开关关闭', 409)
            template = service.store.get_template(template_id)
            if template.version != request['expected_version']:
                raise AutoPostServiceError('x_auto_template_version_conflict', '模板版本已变化，请刷新重试', 409)
            source, ids = service.selector.source, request['material_ids']
            rows = list(source._stream_select(f'SELECT CAST(id AS CHAR) material_id,TRIM(data_source_id) content_id '
                f'FROM `{source.schema}`.ads_custom_source WHERE id IN ({",".join("%s" for _ in ids)})', tuple(ids)))
            times = release_times(source._stream_select, source.schema, source.app_id,
                                  [str(r['content_id']) for r in rows if r.get('content_id')])
            candidates, rejected = {}, []
            unavailable = set(service.selector.history.seen_material_ids(ids)) | set(service.store.reserved_material_ids(ids))
            with future_scope(times):
                for mid in ids:
                    try:
                        if mid in unavailable:
                            raise AutoPostServiceError('material_already_reserved', '素材已有发布或占用记录', 409)
                        item = dict(service.selector.material_validator.validate(mid))
                        if str(item.get('material_id')) != mid:
                            raise AutoPostServiceError('material_identity_mismatch', '素材 ID 核对失败', 409)
                        timing(times, item['content_id'], item['material_language'], service._now())
                        candidates[mid] = item
                    except Exception as exc:
                        rejected.append(dict(material_id=mid, reason=getattr(exc, 'code', 'material_validation_failed')))
            accounts, skipped = [], []
            for aid in template.config.get('account_ids', []):
                try:
                    account = service._account_snapshot(aid)
                    if not account.get('publish_eligible'):
                        raise ValueError('account_not_publishable')
                    accounts.append(account)
                except Exception:
                    skipped.append(dict(account_id=aid, reason='account_not_publishable'))
            catalog = catalog_labels(source, [SimpleNamespace(content_id=i['content_id']) for i in candidates.values()])
            snapshot = portrait_snapshot([a.get('x_user_id') for a in accounts])
            scores, audits, reasons = {}, {}, {}
            for account in accounts:
                aid, lang = str(account['id']), service._account_language(account)
                decision = Decision(account, ['hit-material',key], snapshot)
                for mid,item in candidates.items():
                    metadata = catalog.get((item['content_id'], language(lang)))
                    if target_language(lang) != target_language(item['material_language']):
                        continue
                    reason = match_reason(decision, metadata)
                    if reason:
                        reasons.setdefault(mid, reason)
                        continue
                    if service.store.template_drama_in_cooldown(template.id, item['content_id'],
                            int(template.config.get('cooldown_days') or 0), account_id=aid, now=service._now()):
                        reasons.setdefault(mid, 'drama_cooldown')
                        continue
                    duration = float(item.get('source_duration') or 0)
                    if duration > account_duration_limit_seconds(account):
                        reasons.setdefault(mid, 'account_duration_limit')
                        continue
                    # Occupied/unknown accounts are excluded by the trusted account bridge.
                    scores[mid,aid] = profile_score(decision, metadata)
                    audit = decision.audit(metadata)
                    audit.update(selection_mode='strict_profile_match', match_score=scores[mid,aid])
                    audits[mid,aid] = audit
            allocation = assign(ids, [str(a['id']) for a in accounts], scores)
            rejected += [dict(material_id=mid,reason=reasons.get(mid,'no_available_profile_match')) for mid in candidates if mid not in allocation]
            if not allocation:
                error = AutoPostServiceError('hit_material_no_matching_channel', '没有素材与可发布频道满足一对一画像匹配，未创建任务', 409)
                error.conflicts = rejected
                raise error
            by_language, targets, assignments = {}, {}, {}
            now = service._now()
            for mid,aid in allocation.items():
                item = candidates[mid]
                by_language.setdefault(target_language(item['material_language']), []).append(item['content_id'])
                targets[aid] = [item['content_id']]
                assignments[aid] = dict(material_id=mid,content_id=item['content_id'],profile=audits[mid,aid],
                    timing=timing(times,item['content_id'],item['material_language'],now))
            frozen = dict(request=request, account_ids=list(allocation.values()), account_content_ids=targets,
                          assignments=assignments, release_times=times, timing_policy=POLICY,
                          target=dict(by_language=by_language,content_ids=sorted({i['content_id'] for i in candidates.values()})),
                          skipped_materials=rejected, skipped=skipped)
            run = service.store.create_run(run_key=key,template_id=template.id,template_version=template.version,
                trigger_type='manual',scheduled_at_utc=now,shanghai_date=now.astimezone(BEIJING_TZ).date().isoformat(),
                publish_time=now.astimezone(BEIJING_TZ).strftime('%H:%M'),actor=actor,
                blacklist_snapshot={**_blacklist_dict(source.blacklist_snapshot()),MARKER:frozen})
            service._run_tasks(run,dict(template.config,account_ids=frozen['account_ids']))
            created = True
    service._kick()
    return dict(ok=True,run_id=run.id,created=created,idempotent=not created,matched_channels=len(frozen['account_ids']),
                skipped_channels=frozen['skipped'],skipped_materials=frozen['skipped_materials'],
                assignments=[dict(account_id=aid,**entry) for aid,entry in frozen['assignments'].items()])
