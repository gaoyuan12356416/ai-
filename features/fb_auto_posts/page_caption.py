"""Freeze managed Page copy ahead of the automatic-template fallback."""
import hashlib
import re
import sqlite3

MACROS={'name':'drama_name','drama_name':'drama_name','desc':'desc','url':'url',
        'material_name':'material_name','content_id':'content_id'}
PATTERN=re.compile(r'\{\{([a-z_]+)\}\}|\{([a-z_]+)\}')

def handles(config):
    # Preserve the other product's independently frozen promotion receipts.
    return '{m_url}' not in str(config.get('message_template') or '')


def normalize(text):
    if not isinstance(text,str) or not text.strip():raise ValueError('page_caption_empty')
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',text):raise ValueError('page_caption_invalid')
    names=[a or b for a,b in PATTERN.findall(text)]
    rest=PATTERN.sub('',text)
    if '{' in rest or '}' in rest or set(names)-set(MACROS):raise ValueError('page_caption_macro_invalid')
    if names.count('url')>1:raise ValueError('page_caption_url_repeated')
    return PATTERN.sub(lambda m:'{{'+MACROS[m[1] or m[2]]+'}}',text.replace('\r\n','\n'))

def load(conn,creator_id,page_ids):
    result={'pages':{},'reason':'page_not_configured'}
    try:
        # This legacy auto module has no tenant column. Derive its namespace
        # only from the template creator's server-authored Page records, never
        # a Page's name/language or a first arbitrary tenant. Ambiguity falls back.
        tenants=[r[0] for r in conn.execute('SELECT DISTINCT tenant_key FROM fb_video_page WHERE created_by=? LIMIT 2',(creator_id,))]
        if len(tenants)!=1:return dict(result,reason='page_caption_tenant_unresolved')
        ids=list(dict.fromkeys(str(pid) for pid in page_ids))
        for start in range(0,len(ids),400):
            batch=ids[start:start+400]
            rows=conn.execute('SELECT page_id,config_instance_id,version,caption_template FROM fb_video_page WHERE tenant_key=? AND deleted=0 AND page_id IN ('+','.join('?' for _ in batch)+')',[tenants[0],*batch])
            for pid,instance,version,text in rows:
                record=dict(page_id=pid,config_instance_id=instance,version=version,original_template=text)
                try:record['template']=normalize(text)
                except ValueError as exc:record['reason']=str(exc)
                result['pages'][pid]=record
    except sqlite3.Error:return dict(pages={},reason='page_caption_lookup_unavailable')
    return result

def needs_description(snapshot):
    return any('{{desc}}' in row.get('template','') for row in snapshot['pages'].values())


def description_required(config,page_id,snapshot):
    if '{{desc}}' not in config.get('message_template',''):return False
    row=snapshot['pages'].get(str(page_id),{})
    return not handles(config) or not row.get('template') or '{{desc}}' in row['template']


def source_config(config,pages,snapshot):
    result=dict(config,_page_caption_description=needs_description(snapshot) or '{{desc}}' in config.get('message_template',''))
    if handles(config) and any(not description_required(config,p.page_id,snapshot) for p in pages):
        result['message_template']=config['message_template'].replace('{{desc}}','')
    return result


def candidate_snapshots(config,pages,materials,snapshot):
    """Share bounded reads by language and whether description is mandatory.

    Keep strict and optional pools separate so missing-description rows cannot
    crowd valid fallback candidates out of the existing top-N bound.
    """
    from .languages import candidate_snapshots_for_pages,page_language
    groups={}
    for page in pages:
        groups.setdefault(description_required(config,page.page_id,snapshot),[]).append(page)
    result={}
    for grouped in groups.values():
        by_language=candidate_snapshots_for_pages(source_config(config,grouped,snapshot),grouped,materials)
        result.update({p.page_id:by_language[page_language(p.language)] for p in grouped if page_language(p.language) in by_language})
    return result

def choose(config,page_id,material,snapshot):
    row=snapshot['pages'].get(str(page_id))
    reason=snapshot['reason'] if row is None else row.get('reason','')
    if row and not reason:
        for macro,field in (('desc','drama_description'),('drama_name','drama_name'),
                            ('material_name','material_name'),('content_id','content_id')):
            if '{{'+macro+'}}' in row['template'] and not str(getattr(material,field,'') or '').strip():
                reason='page_caption_'+macro+'_unavailable';break
    audit=dict(source='auto_template',fallback_reason=reason)
    if row:
        audit.update(config_instance_id=row['config_instance_id'],page_version=row['version'])
    if not reason and row:
        audit.update(source='page_management',page_template_sha256=hashlib.sha256(row['original_template'].encode()).hexdigest())
        return dict(config,message_template=row['template']),audit
    return config,audit


def material_eligible(config,page_id,material,snapshot):
    if not handles(config):return True
    selected,_=choose(config,page_id,material,snapshot)
    return '{{desc}}' not in selected['message_template'] or bool(str(getattr(material,'drama_description','') or '').strip())


def freeze(store,conn,config,page,material,task_id,timestamp,snapshot):
    selected,audit=choose(config,page.page_id,material,snapshot)
    from .links import build_short_url
    short=build_short_url(task_id) if '{{url}}' in selected['message_template'] else ''
    try:message=store._message(selected,material,short,strict_length=audit['source']=='page_management')
    except RuntimeError as exc:
        if audit['source']!='page_management' or getattr(exc,'code',None)!='fb_auto_message_length_invalid':raise
        selected=config;audit.update(source='auto_template',fallback_reason='page_caption_expanded_too_long')
        short=build_short_url(task_id) if '{{url}}' in selected['message_template'] else ''
        message=store._message(selected,material,short)
    if '{{desc}}' in selected['message_template'] and not str(getattr(material,'drama_description','') or '').strip():
        from .core import StoreError
        raise StoreError('fb_auto_caption_source_missing','发布文案缺少必需的短剧简介，未创建任务',409)
    long=''
    if short:short,long=store._link_values(page,material,task_id,timestamp)
    conn.execute('UPDATE fb_auto_task SET message_text=?,short_url=?,long_url=? WHERE id=?',(message,short,long,task_id))
    audit['effective_template_sha256']=hashlib.sha256(selected['message_template'].encode()).hexdigest()
    audit['message_sha256']=hashlib.sha256(message.encode()).hexdigest()
    return audit
