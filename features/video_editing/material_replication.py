"""Manual, one-material B-group submissions with a durable, non-retrying outbox.

The API never loads B credentials or calls B. Only MaterialReplicationWorker
crosses the outbound boundary. Acceptance is a snapshot, not production progress.
"""
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

import requests

from . import VideoEditingError, require
from .material_source import ReadOnlyCatalog, _language, _literal, _positive
from .store import encode, identifier, now, owner

PROTECTED = {'pending', 'submitting', 'accepted', 'partial', 'unknown_outcome'}
ENDPOINT = 'https://marketing.mydramawave.com/api/internal/material/a-group-task-create'
LANGUAGE_NAMES = dict(en='英语', es='西班牙语', pt='葡萄牙语', fr='法语', de='德语', it='意大利语',
    ja='日语', ko='韩语', id='印尼语', th='泰语', vi='越南语', ar='阿拉伯语', tr='土耳其语',
    ru='俄语', pl='波兰语', cs='捷克语', ro='罗马尼亚语', el='希腊语',
    **{'zh-TW':'繁体中文', 'zh-CN':'简体中文'})
INPUT_FIELDS = {'operation_id', 'task_type', 'audio_type', 'target_material_type', 'target_langs',
                'operator', 'descant_start_time', 'descant_end_time', 'no_bgm'}
CONFLICT_FIELDS = ('operator', 'resource_id', 'series_langs', 'target_material_type',
                   'descant_start_time', 'descant_end_time', 'no_bgm', 'orig_materials')


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def language(value):
    require(isinstance(value, str), 'replication_language_invalid', '语种代码无效')
    code = _language(value)
    code = {'zh-tw':'zh-TW', 'zh-cn':'zh-CN'}.get(code, code)
    require(bool(re.fullmatch(r'[a-z]{2,3}(?:-[A-Z]{2})?', code)),
            'replication_language_invalid', '语种代码无效')
    return code


def languages(values):
    require(isinstance(values, list) and 1 <= len(values) <= 30,
            'replication_languages_required', '请选择 1 至 30 个目标语种')
    return sorted({language(value) for value in values})


def account(value):
    require(isinstance(value, str) and 0 < len(value.strip()) <= 255 and
            not any(ord(c) < 32 or ord(c) == 127 for c in value),
            'replication_operator_invalid', '请输入有效的盈量账号')
    return value.strip()


def timecode(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{2}:[0-5]\d:[0-5]\d(?:\.\d{1,3})?', value),
            'replication_time_invalid', '解说时间须为 HH:MM:SS 或 HH:MM:SS.SSS')
    hours, minutes, seconds = value.split(':')
    seconds, _, fraction = seconds.partition('.')
    millis = (int(hours)*3600 + int(minutes)*60 + int(seconds))*1000 + int((fraction+'000')[:3])
    canonical = '%02d:%02d:%02d' % (int(hours), int(minutes), int(seconds))
    if millis % 1000:
        canonical += '.%03d' % (millis % 1000)
    return canonical, millis


def intent(payload):
    require(isinstance(payload, dict) and set(payload) <= INPUT_FIELDS,
            'replication_payload_invalid', '请求包含不支持的字段')
    op = payload.get('operation_id')
    require(isinstance(op, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}', op),
            'operation_id_required', '请提供操作标识')
    result = {}
    for field, default, choices in (('task_type',1,{1,3,4}), ('audio_type',0,{0,1,2}),
                                     ('target_material_type',1,{1,2})):
        value = payload.get(field, default)
        require(type(value) is int and value in choices, 'replication_option_invalid', '制作选项无效')
        result[field] = value
    result['target_langs'] = languages(payload.get('target_langs'))
    result['operator'] = account(payload['operator']) if payload.get('operator') is not None else None
    result['no_bgm'] = payload.get('no_bgm', False)
    require(type(result['no_bgm']) is bool, 'replication_option_invalid', '母素材无背景音乐选项无效')
    result['descant_start_time'] = result['descant_end_time'] = ''
    if result['target_material_type'] == 2:
        start, start_ms = timecode(payload.get('descant_start_time'))
        end, end_ms = timecode(payload.get('descant_end_time'))
        require(end_ms > start_ms, 'replication_time_invalid', '解说结束时间须晚于开始时间')
        result.update(descant_start_time=start, descant_end_time=end)
    return op, result


def canonical_request(request):
    """Normalize optional defaults and language sets, including audited imports."""
    result = dict(request)
    result['target_langs'] = languages(result.get('target_langs'))
    require(isinstance(result.get('series_langs'), list) and len(result['series_langs']) == 1,
            'replication_source_invalid', '源语种必须唯一')
    result['series_langs'] = [language(result['series_langs'][0])]
    result['audio_type'] = result.get('audio_type', 0)
    result['target_material_type'] = result.get('target_material_type', 1)
    result['no_bgm'] = result.get('no_bgm', False)
    if result['target_material_type'] == 1:
        result.pop('descant_start_time', None); result.pop('descant_end_time', None)
    else:
        for key in ('descant_start_time', 'descant_end_time'):
            result[key] = timecode(result.get(key))[0]
    return result


def b_key(request):
    # B's documented key deliberately excludes operator/resource/narration/no_bgm.
    return digest({'url':request['orig_materials'][0]['url'], 'audio_type':request['audio_type'],
                   'task_type':request['task_type'], 'target_langs':sorted(request['target_langs'])})


def safe_error(value):
    """Do not echo possible credentials or material URLs from an upstream error."""
    text = str(value or '')
    text = re.sub(r'https?://\S+', '[地址已隐藏]', text)
    text = re.sub(r'(?i)(bearer\s+|token\s*[:=]\s*)\S+', '[凭据已隐藏]', text)
    return ''.join(c for c in text if ord(c) >= 32)[:500]


def safe_response(value, secret=''):
    """Retain the B body for audit without headers, credentials or media URLs."""
    if isinstance(value, dict):
        return {str(key):('[已隐藏]' if re.search(r'(?i)authorization|headers|token|password|secret',str(key))
                          else safe_response(item,secret)) for key,item in value.items()}
    if isinstance(value, list):
        return [safe_response(item,secret) for item in value]
    if isinstance(value, str):
        if secret:
            value=value.replace(secret,'[凭据已隐藏]')
        value=re.sub(r'https?://\S+','[地址已隐藏]',value)
        return re.sub(r'(?i)(bearer\s+|token\s*[:=]\s*)\S+','[凭据已隐藏]',value)
    return value if value is None or type(value) in {bool,int,float} else str(value)


def summary(history):
    live = next((item for item in history if not item['historical']),None)
    return live or next((item for item in history if item['status'] in {'accepted','partial'}),history[0] if history else None)


def receipt(request, response, http_status=200):
    """Classify only a coherent acceptance or explicit pre-acceptance rejection."""
    unknown = {'error':'接口受理结果不确定，请人工核实；不会自动重发'}
    if http_status != 200 or not isinstance(response, dict):
        return 'unknown_outcome', unknown
    code = response.get('code')
    if type(code) is int and code in {400,401}:
        return 'rejected', {'error':safe_error(response.get('message') or response.get('err_msg') or '接口拒绝了请求'),
                            'response_code':code}
    if code != 200 or type(code) is not int or not isinstance(response.get('data'), dict):
        return 'unknown_outcome', unknown
    data = response['data']
    try:
        effective = languages(data.get('target_langs'))
        unsupported = data.get('unsupported_langs', [])
        require(isinstance(unsupported, list), 'invalid_receipt', '')
        unsupported = sorted({language(value) for value in unsupported})
        requested = set(request['target_langs'])
        require(set(effective).isdisjoint(unsupported) and set(effective) | set(unsupported) == requested,
                'invalid_receipt', '')
        require(data.get('operator') == request['operator'] and _positive(data.get('task_id')) and
                data.get('status') in {'queued','waiting_download','duplicate'}, 'invalid_receipt', '')
        require([language(value) for value in data.get('series_langs', [])] == request['series_langs'],
                'invalid_receipt', '')
        ids = data.get('task_material_ids')
        require(isinstance(ids, list) and len(ids) == 1 and all(_positive(v) for v in ids), 'invalid_receipt', '')
        items = data.get('items')
        require(isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict), 'invalid_receipt', '')
        item = items[0]
        require(str(item.get('material_id')) == str(request['orig_materials'][0]['material_id']) and
                str(item.get('task_material_id')) == str(ids[0]) and not item.get('err_msg') and
                isinstance(item.get('status'),str) and 0<len(item['status'].strip())<=128 and
                not any(ord(c)<32 or ord(c)==127 for c in item['status']), 'invalid_receipt', '')
    except (VideoEditingError, TypeError, ValueError, KeyError):
        return 'unknown_outcome', unknown
    return ('partial' if unsupported else 'accepted'), {
        'error':'', 'response_code':200, 'effective_langs':effective, 'unsupported_langs':unsupported,
        'upstream_status':data['status'], 'upstream_item_status':item['status'], 'b_task_id':int(data['task_id']),
        'b_task_material_ids':[int(value) for value in ids]}


class ReplicationCatalog(ReadOnlyCatalog):
    def operator(self, email):
        email = str(email or '').strip().lower()
        require(email and '@' in email and len(email) <= 255, 'replication_owner_email_missing',
                '原任务创建人未配置有效邮箱，请管理员填写盈量账号', 409)
        table = '`'+self.database+'`.'
        rows = self.query("SELECT JSON_OBJECT('username',MIN(TRIM(au.username))) FROM "+table+
            'admin_user_group aug JOIN '+table+'admin_users au ON au.id=aug.sub_user_id '
            'WHERE aug.status=0 AND LOWER(TRIM(aug.email))='+_literal(email)+
            " AND TRIM(COALESCE(au.username,''))<>'' GROUP BY BINARY TRIM(au.username) LIMIT 2")
        require(len(rows) == 1, 'replication_operator_ambiguous',
                '原任务创建人的邮箱未唯一对应有效盈量账号，请管理员填写', 409)
        return account(rows[0].get('username'))


class MaterialReplication:
    def __init__(self, service, catalog=None):
        self.service, self.store, self.catalog = service, service.store, catalog

    @property
    def enabled(self):
        return os.environ.get('VIDEO_EDITING_MATERIAL_REPLICATION_ENABLED','0').lower() in {'1','true','yes'} and not Path(
            os.environ.get('VIDEO_EDITING_MATERIAL_REPLICATION_DISABLED_FILE',
                           '/etc/video-editing/material-replication.disabled')).exists()

    def _catalog(self):
        if self.catalog is None:
            self.catalog = ReplicationCatalog(self.service.app)
        return self.catalog

    def metadata(self, task_id, db=None):
        if db is None:
            with closing(self.store.connect()) as connection:
                return self.metadata(task_id, connection)
        row = db.execute('SELECT data FROM ve_material_replication_metadata WHERE task_id=?', (task_id,)).fetchone()
        return json.loads(row['data']) if row else {}

    def default_operator(self, task):
        override = self.metadata(task['id']).get('default_operator_override')
        if override:
            return account(override)
        try:
            tenant, user_id = json.loads(task['owner'])
            app = self.service.app
            with app.JOB_DB_LOCK:
                with closing(app.get_job_db_connection()) as db:
                    rows = db.execute('SELECT email FROM drama_admin_user WHERE tenant_key=? AND user_id=? LIMIT 2',
                                      (tenant, user_id)).fetchall()
            require(len(rows) == 1, 'replication_owner_missing', '未找到原任务创建人，请管理员填写盈量账号', 409)
            email = rows[0][0]
        except VideoEditingError:
            raise
        except Exception:
            raise VideoEditingError('replication_owner_unavailable', '原任务创建人信息暂不可用，请管理员填写盈量账号', 503) from None
        return self._catalog().operator(email)

    def dto(self, row, db=None):
        if row is None:
            return None
        if db is None:
            with closing(self.store.connect()) as connection:
                return self.dto(row, connection)
        data, request = row['data'], row['data']['request']
        operations = db.execute('SELECT operation_id FROM ve_material_replication_operation WHERE record_id=? ORDER BY created_at,operation_id',
                                (row['id'],)).fetchall()
        return {'id':row['id'], 'operation_id':row['operation_id'],
                'operation_ids':[value['operation_id'] for value in operations], 'status':row['status'],
                **{key:request.get(key, '') for key in ('operator','task_type','audio_type','target_material_type',
                    'descant_start_time','descant_end_time')}, 'no_bgm':request.get('no_bgm',False),
                'target_langs':request['target_langs'], 'effective_langs':data.get('effective_langs',[]),
                'unsupported_langs':data.get('unsupported_langs',[]), 'upstream_status':data.get('upstream_status',''),
                'upstream_item_status':data.get('upstream_item_status',''),
                'b_task_id':data.get('b_task_id'), 'b_task_material_ids':data.get('b_task_material_ids',[]),
                'error':data.get('error',''), 'created_at':row['created_at'], 'submitted_at':data.get('submitted_at'),
                'historical':bool(data.get('historical')), 'import_source':row.get('import_source') or ''}

    def history(self, task_id):
        with closing(self.store.connect()) as db:
            rows = db.execute('SELECT r.* FROM ve_material_replication r WHERE r.id IN '
                              '(SELECT id FROM ve_material_replication WHERE task_id=? UNION '
                              'SELECT record_id FROM ve_material_replication_operation WHERE task_id=?) '
                              'ORDER BY r.created_at DESC,r.rowid DESC', (task_id,task_id)).fetchall()
            return [self.dto(self.store.row(row), db) for row in rows]

    def local_source(self, task):
        require(task['kind'] == 'render' and task['status'] == 'completed',
                'replication_not_completed', '成片制作完成并入库后可复刻', 409)
        output_id = task['data'].get('output_id') or 'vo_'+task['id'][3:]
        output = self.store.get('output', output_id)
        upload = self.store.get('material_upload', output_id)
        require(output and output['status'] == 'ready' and output['task_id'] == task['id'] and
                output['data'].get('artifact') == (task['data'].get('result') or {}).get('artifact') and
                bool(output['data'].get('artifact')), 'replication_output_unverified', '成片账本尚未完成校验', 409)
        require(upload and upload['task_id'] == task['id'] and upload['status'] == 'succeeded' and
                _positive(upload['data'].get('material_id')), 'replication_material_missing', '请先将成片上传至素材库', 409)
        request = upload['data'].get('request') or {}
        url = request.get('url')
        try:
            parsed = urlsplit(url) if isinstance(url,str) else None
        except ValueError:
            parsed = None
        require(parsed and 0 < len(url) <= 5000 and parsed.scheme in {'http','https'} and
                parsed.hostname and not parsed.username and not parsed.password,
                'replication_material_url_invalid', '已入库素材没有可用的稳定地址', 409)
        require(not upload['data'].get('artifact') or upload['data']['artifact'] == output['data']['artifact'],
                'replication_output_unverified', '入库记录与成片身份不一致', 409)
        duration = output['data']['artifact'].get('duration_ms')
        require(type(duration) is int and duration > 0, 'replication_duration_missing', '成片缺少有效时长', 409)
        name = request.get('name')
        require(isinstance(name,str) and name.strip(), 'replication_name_missing', '成片缺少素材名称', 409)
        require(len(name)<=128,'replication_name_too_long','已入库素材名称超过接口的 128 字符限制',409)
        variant = upload['data'].get('source_variant') or {}
        resource = variant.get('series_code') or task['data'].get('series_code') or ''
        source = {'material_id':int(upload['data']['material_id']), 'name':name,
                  'resource_id':int(resource) if _positive(resource) else None,
                  'source_language':language(request.get('language')), 'duration_ms':duration}
        return output_id, upload, source, url

    def for_task(self, task):
        # The frequently refreshed task list must not materialize all receipts.
        with closing(self.store.connect()) as db:
            where = ('r.id IN (SELECT id FROM ve_material_replication WHERE task_id=? UNION '
                     'SELECT record_id FROM ve_material_replication_operation WHERE task_id=?)')
            args = (task['id'],task['id'])
            count = db.execute('SELECT COUNT(*) FROM ve_material_replication r WHERE '+where,args).fetchone()[0]
            row = db.execute('SELECT r.* FROM ve_material_replication r WHERE '+where+
                " ORDER BY CASE WHEN COALESCE(json_extract(r.data,'$.historical'),0)=0 THEN 0 "
                "WHEN r.status IN ('accepted','partial') THEN 1 ELSE 2 END,r.created_at DESC,r.rowid DESC LIMIT 1",args).fetchone()
            latest = self.dto(self.store.row(row),db) if row else None
        reason = ''
        try:
            self.local_source(task)
            require(self.enabled, 'replication_disabled', '素材复刻暂未开放', 503)
        except VideoEditingError as exc:
            reason = exc.message
        return {'enabled':not reason, 'disabled_reason':reason, 'summary':latest, 'count':count}

    def context(self, task_id, actor, history_only=False):
        task = self.service.task(task_id, actor)
        history = self.history(task_id)
        result = {'enabled':False, 'disabled_reason':'', 'source':None, 'default_operator':'',
                  'can_change_operator':bool(actor.get('is_admin')), 'operator_error':'', 'target_languages':[],
                  'history':history, 'summary':summary(history)}
        try:
            _, upload, source, _ = self.local_source(task)
            result['source'] = source
            metadata = self.metadata(task_id)
            if not source['resource_id'] and metadata.get('source'):
                source['resource_id'] = metadata['source'].get('resource_id')
            result['default_operator'] = metadata.get('default_operator_override','')
            require(self.enabled, 'replication_disabled', '素材复刻暂未开放', 503)
            if history_only:
                result['enabled'] = True
                return result
            variants = self.source_context(task, upload, source)
            result['target_languages'] = [{'code':code, 'name':LANGUAGE_NAMES.get(code,code)} for code in variants]
            try:
                result['default_operator'] = self.default_operator(task)
            except VideoEditingError as exc:
                result['operator_error'] = exc.message
                if not actor.get('is_admin'):
                    raise
            result['enabled'] = True
        except VideoEditingError as exc:
            result['disabled_reason'] = exc.message
        except Exception:
            result['disabled_reason'] = '素材复刻目录暂不可用，请稍后重试'
        return result

    def source_context(self, task, upload, source):
        catalog = self.service.catalog
        require(catalog is not None, 'replication_catalog_unavailable', '资源语种目录暂不可用', 503)
        variant = upload['data'].get('source_variant') or {}
        app_id = variant.get('app_id') or task['data'].get('app_id')
        content_id = variant.get('content_id') or task['data'].get('content_id')
        require(app_id and content_id, 'replication_source_identity_missing', '缺少原剧版本，无法确定资源 ID', 409)
        selected = catalog.variant(str(app_id), str(content_id),
                                   series_code=str(source['resource_id']) if source['resource_id'] else None)
        resource = selected.get('series_code')
        require(_positive(resource) and int(resource) <= 2**63-1, 'replication_resource_invalid', '原剧资源 ID 不是有效整数', 409)
        require(language(selected.get('language')) == source['source_language'],
                'replication_source_language_mismatch', '原剧目录语种与已入库素材不一致', 409)
        source['resource_id'] = int(resource)
        variants = catalog.resource_variants(str(resource))
        candidates = {source['source_language']}
        for item in variants:
            if str(item.get('series_code')) == str(resource) and item.get('language'):
                candidates.add(language(item['language']))
        return sorted(candidates)

    def _operation(self, task_id, actor_owner, op, payload_hash, db):
        row = db.execute('SELECT * FROM ve_material_replication_operation WHERE owner=? AND operation_id=?',
                         (actor_owner, op)).fetchone()
        if row is None:
            return None
        require(row['task_id'] == task_id and row['payload_hash'] == payload_hash,
                'replication_operation_conflict', '同一操作标识不能用于不同的制作参数', 409)
        result = self.dto(self.store.get('material_replication', row['record_id'], db), db)
        result.update(json.loads(row['data']))
        result.update(reused=True, submitted_operation_id=op)
        return result

    def _remember(self, task_id, actor_owner, op, payload_hash, record_id, extra, db):
        db.execute('INSERT INTO ve_material_replication_operation(owner,operation_id,task_id,payload_hash,record_id,data,created_at) '
                   'VALUES(?,?,?,?,?,?,?)', (actor_owner,op,task_id,payload_hash,record_id,encode(extra),now()))

    def enqueue(self, task_id, actor, payload):
        task = self.service.task(task_id, actor)
        op, params = intent(payload)
        actor_owner, payload_hash = owner(actor), digest(params)
        with closing(self.store.connect()) as db:
            previous = self._operation(task_id, actor_owner, op, payload_hash, db)
            if previous:
                return previous
        require(self.enabled, 'replication_disabled', '素材复刻暂未开放', 503)
        output_id, upload, source, url = self.local_source(task)
        candidates = self.source_context(task, upload, source)
        require(set(params['target_langs']) <= set(candidates), 'replication_target_invalid', '目标语种须从本剧资源的语种中选择')
        chosen = params['operator']
        if actor.get('is_admin') and chosen:
            operator = chosen
        else:
            operator = self.default_operator(task)
            require(chosen in {None,operator}, 'replication_operator_forbidden', '只有管理员可以修改归属账号', 403)
        request = {key:value for key,value in params.items() if key != 'operator'}
        request.update(operator=operator, resource_id=source['resource_id'], series_langs=[source['source_language']],
                       orig_materials=[{'material_id':source['material_id'], 'name':source['name'], 'url':url,
                                        'duration':source['duration_ms']}])
        if request['target_material_type'] == 2:
            require(timecode(request['descant_end_time'])[1] <= source['duration_ms'],
                    'replication_time_out_of_range', '解说结束时间不能超过成片时长')
        request = canonical_request(request)
        request_hash, key = digest(request), b_key(request)
        with self.store.transaction() as db:
            previous = self._operation(task_id, actor_owner, op, payload_hash, db)
            if previous:
                return previous
            require(self.enabled, 'replication_disabled', '素材复刻暂未开放', 503)
            # No broad material lock: only B's exact dedup key protects an existing submission.
            rows = db.execute('SELECT * FROM ve_material_replication WHERE b_dedup_key=? ORDER BY created_at DESC,rowid DESC', (key,)).fetchall()
            prior = next((self.store.row(row) for row in rows if row['status'] in PROTECTED), None)
            if prior:
                require(prior['task_id'] == task_id or actor.get('is_admin') or prior['owner'] == actor_owner,
                        'replication_existing_submission', '该素材已有复刻受理记录，请联系管理员核实', 409)
                extra = {'reused':True}
                if prior['request_hash'] != request_hash:
                    old = prior['data']['request']
                    diffs = []
                    for field in CONFLICT_FIELDS:
                        if old.get(field) != request.get(field):
                            before, after = old.get(field), request.get(field)
                            if field == 'orig_materials':
                                before = [{k:v for k,v in item.items() if k != 'url'} for item in before]
                                after = [{k:v for k,v in item.items() if k != 'url'} for item in after]
                            diffs.append({'field':field, 'previous':before, 'requested':after})
                    extra.update(conflict=True, conflict_fields=diffs)
                self._remember(task_id,actor_owner,op,payload_hash,prior['id'],extra,db)
                result = self.dto(prior,db); result.update(extra,submitted_operation_id=op)
                return result
            record_id = identifier('vmr_')
            self.store.insert('material_replication', {'id':record_id, 'task_id':task_id, 'output_id':output_id,
                'owner':actor_owner, 'operation_id':op, 'request_hash':request_hash, 'b_dedup_key':key,
                'status':'pending', 'data':{'request':request,'source':source,'attempts':0,'error':'','historical':False}}, db)
            self._remember(task_id,actor_owner,op,payload_hash,record_id,{},db)
            return self.dto(self.store.get('material_replication',record_id,db),db)

    def import_history(self, task_id, request, response, *, import_source, submitted_at,
                       operation_id=None, default_operator_override=None):
        """Import an independently verified audit receipt; never queue or issue HTTP.

        The caller verifies audit-file hashes and provenance first. This helper
        additionally checks the current successful material identity and freezes
        both receipt and any explicitly reviewed operator override in new tables.
        """
        require(isinstance(import_source,str) and 0 < len(import_source) <= 2000 and
                type(submitted_at) is int and submitted_at > 0, 'replication_import_invalid', '历史来源或时间无效')
        task = self.store.get('task',task_id)
        require(task is not None, 'task_not_found', '制作任务不存在',404)
        output_id, _, source, url = self.local_source(task)
        request = canonical_request(request)
        materials = request.get('orig_materials')
        require(isinstance(materials,list) and len(materials) == 1 and
                str(materials[0].get('material_id')) == str(source['material_id']) and materials[0].get('url') == url and
                materials[0].get('name') == source['name'] and materials[0].get('duration') == source['duration_ms'],
                'replication_import_source_mismatch', '历史回执与已入库母素材不一致',409)
        require(_positive(request.get('resource_id')) and request['series_langs'] == [source['source_language']],
                'replication_import_source_mismatch', '历史回执的资源或源语种无效',409)
        require(not source['resource_id'] or source['resource_id']==int(request['resource_id']),
                'replication_import_source_mismatch','历史回执的资源与已入库版本不一致',409)
        status, details = receipt(request,response)
        require(status in {'accepted','partial','rejected'}, 'replication_import_receipt_invalid', '历史受理回执不完整',409)
        override = account(default_operator_override) if default_operator_override is not None else None
        require(not override or status in {'accepted','partial'} and override == request['operator'],
                'replication_import_override_invalid', '归属覆盖须来自已核实的成功受理回执',409)
        fingerprint = digest({'request':request,'response':response,'submitted_at':submitted_at,'override':override})
        op = operation_id or 'history_'+digest(import_source)[:40]
        require(re.fullmatch(r'[A-Za-z0-9_-]{8,128}',op), 'operation_id_required', '请提供操作标识')
        source['resource_id'] = int(request['resource_id'])
        with self.store.transaction() as db:
            existing = db.execute('SELECT * FROM ve_material_replication WHERE import_source=?',(import_source,)).fetchone()
            if existing:
                existing = self.store.row(existing)
                require(existing['task_id'] == task_id and existing['data'].get('import_fingerprint') == fingerprint,
                        'replication_import_conflict', '同一历史来源不能导入不同内容',409)
                return {**self.dto(existing,db),'reused':True}
            require(not db.execute('SELECT 1 FROM ve_material_replication_operation WHERE owner=? AND operation_id=?',
                                   (task['owner'],op)).fetchone(), 'replication_operation_conflict','历史操作标识已使用',409)
            if status in {'accepted','partial'}:
                protected = db.execute("SELECT id FROM ve_material_replication WHERE b_dedup_key=? AND status<>'rejected' LIMIT 1",
                                       (b_key(request),)).fetchone()
                require(not protected,'replication_import_existing','该历史请求已存在受理记录，需先核对账本',409)
            record_id = identifier('vmr_')
            self.store.insert('material_replication', {'id':record_id,'task_id':task_id,'output_id':output_id,
                'owner':task['owner'],'operation_id':op,'request_hash':digest(request),'b_dedup_key':b_key(request),
                'import_source':import_source,'status':status,'created_at':submitted_at,'updated_at':submitted_at,
                'data':{'request':request,'source':source,'historical':True,'import_fingerprint':fingerprint,
                        'attempts':1,'submitted_at':submitted_at,'attempt_started_at':submitted_at,
                        'http_status':200,'response_received_at':submitted_at,'response_body':safe_response(response),**details}},db)
            self._remember(task_id,task['owner'],op,fingerprint,record_id,{},db)
            if override:
                metadata = self.metadata(task_id,db)
                metadata.update(default_operator_override=override,operator_override_import_source=import_source,source=source)
                db.execute('INSERT INTO ve_material_replication_metadata(task_id,data,created_at,updated_at) VALUES(?,?,?,?) '
                           'ON CONFLICT(task_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                           (task_id,encode(metadata),now(),now()))
            return self.dto(self.store.get('material_replication',record_id,db),db)


class MaterialReplicationWorker:
    """One worker/lease, one HTTP attempt. Unknown outcomes are manual-only."""
    def __init__(self, material, http=None):
        self.material, self.store, self.http = material, material.store, http
        self.holder = identifier('replication_')

    def settings(self):
        # Only invoked by the independent worker, never by an API/list route.
        base = os.environ.get('A_GROUP_TASK_CREATE_BASE_URL','').strip().rstrip('/')
        token = os.environ.get('A_GROUP_TASK_CREATE_TOKEN','').strip()
        require(base in {'https://marketing.mydramawave.com',ENDPOINT} and token,
                'replication_worker_unconfigured','素材复刻 worker 凭据未配置',503)
        return ENDPOINT, token

    def tick(self):
        if not self.store.lease(self.holder,ttl=120,name='material_replication'):
            return False
        with closing(self.store.connect()) as db:
            # A crash after the durable outbound marker can never authorize a re-POST.
            row = db.execute("SELECT * FROM ve_material_replication WHERE status='submitting' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if row is None and self.material.enabled:
                row = db.execute("SELECT * FROM ve_material_replication WHERE status='pending' ORDER BY created_at,rowid LIMIT 1").fetchone()
        if row is None:
            return False
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(30):
                try:
                    self.store.lease(self.holder,ttl=120,name='material_replication')
                except Exception:
                    logging.warning('material replication lease heartbeat failed')
        thread = threading.Thread(target=heartbeat,daemon=True); thread.start()
        try:
            self.process(self.store.row(row))
        finally:
            stop.set(); thread.join(timeout=1)
        return True

    def process(self, row):
        if row['status'] == 'submitting':
            self.finish(row['id'],'unknown_outcome',{'error':'提交过程中断，受理结果待人工核实；不会自动重发'})
            return
        if row['status'] != 'pending' or not self.material.enabled:
            return
        endpoint, token = self.settings()
        with self.store.transaction() as db:
            current = self.store.get('material_replication',row['id'],db)
            if current['status'] != 'pending' or current['data'].get('attempts') or not self.material.enabled:
                return
            stamp = now()
            data = dict(current['data']); data.update(attempts=1,submitted_at=stamp,attempt_started_at=stamp,error='')
            self.store.update('material_replication',row['id'],{'status':'submitting','data':data},db)
        http_status, body, received_at = None, None, None
        try:
            if self.http is not None:
                response = self.http.post(endpoint,json=data['request'],headers={'token':token},
                                          timeout=(5,60),allow_redirects=False)
            else:
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.post(endpoint,json=data['request'],headers={'token':token},
                                            timeout=(5,60),allow_redirects=False)
            http_status, received_at = response.status_code, now()
            try:
                body = response.json()
            except ValueError:
                body = str(getattr(response,'text',''))[:1024*1024]
            status, details = receipt(data['request'],body,http_status)
            if details.get('error'):
                details['error'] = details['error'].replace(token,'[凭据已隐藏]')
        except Exception:
            status, details = 'unknown_outcome', {'error':'接口连接或响应中断，受理结果待人工核实；不会自动重发'}
        details.update(http_status=http_status,response_received_at=received_at,response_body=safe_response(body,token))
        self.finish(row['id'],status,details)

    def finish(self, record_id, status, details):
        with self.store.transaction() as db:
            current = self.store.get('material_replication',record_id,db)
            if current is None or current['status'] != 'submitting':
                return
            data = dict(current['data']); data.update(details)
            self.store.update('material_replication',record_id,{'status':status,'data':data},db)
