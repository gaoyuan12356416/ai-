"""Resource-based drafts; incomplete edits are saved, render failures are local."""
import copy
import hashlib
import re
from contextlib import closing

from . import VideoEditingError, require
from .store import can_manage, encode, identifier, owner
from features.drama_synthesis.core import DramaSynthesisError

ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
SOURCE_ID = re.compile(r'(?:va_|vep_)[0-9a-f]{32}')


def normalize_draft(payload):
    require(isinstance(payload,dict) and payload.get('schema_version')==2,'draft_invalid','剪辑草稿格式无效')
    require(not(set(payload)-{'schema_version','name','units','expected_version','operation_id'}),'draft_fields_invalid','剪辑内容包含未知字段')
    name=payload.get('name','')
    require(isinstance(name,str) and len(name)<=120,'name_invalid','项目名称最多120字')
    units=payload.get('units',[])
    require(isinstance(units,list) and len(units)<=20,'unit_count_invalid','最多20个制作单元')
    clean=[];seen=set()
    for unit_index,unit in enumerate(units):
        require(isinstance(unit,dict) and not(set(unit)-{'id','series_code','title','clips'}),'unit_invalid','制作单元格式无效')
        uid=unit.get('id') or 'unit_%03d'%unit_index
        require(isinstance(uid,str) and ID.fullmatch(uid) and uid not in seen,'unit_id_invalid','制作单元编号无效或重复')
        seen.add(uid)
        code=unit.get('series_code','');title=unit.get('title','')
        require(isinstance(code,str) and len(code)<=255 and not any(ord(c)<32 for c in code),'resource_invalid','资源编号格式无效')
        require(isinstance(title,str) and len(title)<=200,'unit_title_invalid','制作单元名称最多200字')
        clips=unit.get('clips',[])
        require(isinstance(clips,list) and len(clips)<=300,'clip_count_invalid','单元最多300个片段')
        output=[];clip_ids=set()
        for clip_index,clip in enumerate(clips):
            allowed={'id','source_id','start_ms','end_ms','fit','mute','transition','mirror','subtitle_band'}
            require(isinstance(clip,dict) and not(set(clip)-allowed),'clip_invalid','剪辑片段字段无效')
            cid=clip.get('id') or 'clip_%03d'%clip_index;sid=clip.get('source_id','')
            require(isinstance(cid,str) and ID.fullmatch(cid) and cid not in clip_ids,'clip_id_invalid','片段编号无效或重复')
            require(isinstance(sid,str) and (not sid or SOURCE_ID.fullmatch(sid)),'source_id_invalid','素材编号格式无效')
            clip_ids.add(cid)
            for field in ('start_ms','end_ms'):
                value=clip.get(field)
                require(value is None or type(value) is int and abs(value)<=2**53-1,'clip_range_invalid','片段时间必须为整数毫秒')
            require(clip.get('fit','contain') in {'contain','cover'} and clip.get('transition','cut') in {'cut','fade'}
                    and type(clip.get('mute',False)) is bool and type(clip.get('mirror',False)) is bool,'clip_style_invalid','片段样式格式无效')
            band=clip.get('subtitle_band',{'top':960,'bottom':1280} if clip.get('mirror') else None)
            require(band is None or isinstance(band,dict) and set(band)=={'top','bottom'} and
                    all(type(band[k]) is int and abs(band[k])<=2**53-1 for k in band),'subtitle_band_invalid','字幕保护范围格式无效')
            output.append({'id':cid,'source_id':sid,'start_ms':clip.get('start_ms'),'end_ms':clip.get('end_ms'),
                           'fit':clip.get('fit','contain'),'mute':clip.get('mute',False),'transition':clip.get('transition','cut'),
                           'mirror':clip.get('mirror',False),'subtitle_band':copy.deepcopy(band)})
        clean.append({'id':uid,'series_code':code.strip(),'title':title,'clips':output})
    return {'schema_version':2,'name':name.strip() or '未命名剪辑项目','units':clean}


def issue(unit,code,message,source_id=None):
    result={'unit_id':unit['id'],'code':code,'message':message}
    if source_id:result['source_id']=source_id
    return result


def readable(row,actor):
    return bool(row and row['status']!='deleted' and
                (row['kind']=='episode' or row['status']=='ready' or can_manage(row,actor)))


def draft_issues(service,draft,actor):
    """Local-only hints: saving a draft never depends on remote catalog health."""
    result=[]
    for unit in draft.get('units',[]):
        if not unit.get('series_code'):result.append(issue(unit,'resource_required','请选择目标资源'))
        if not unit.get('clips'):result.append(issue(unit,'timeline_empty','请添加至少一个片段'))
        for clip in unit.get('clips',[]):
            sid=clip.get('source_id','');row=service.store.get('source',sid)
            if not readable(row,actor):
                result.append(issue(unit,'source_unavailable','素材不存在或尚不可使用',sid));continue
            data=row['data'];code=data.get('series_code','')
            if row['kind']=='asset' and not code:result.append(issue(unit,'source_resource_required','素材待补资源标签',sid))
            elif code and code!=unit.get('series_code'):result.append(issue(unit,'clip_resource_mismatch','素材与目标资源不匹配',sid))
            if row['status']!='ready':result.append(issue(unit,'source_not_ready','素材仍在上传或媒体校验中',sid))
            start,end=clip.get('start_ms'),clip.get('end_ms');duration=data.get('metadata',{}).get('duration_ms')
            if type(start) is not int or type(end) is not int or not 0<=start<end or duration and end>duration:
                result.append(issue(unit,'clip_range_invalid','片段保留范围待修正',sid))
            band=clip.get('subtitle_band')
            if clip.get('mirror') and band is not None and not 0<=band['top']<band['bottom']<=1280:
                result.append(issue(unit,'subtitle_band_invalid','字幕保护范围待修正',sid))
    return result


def upgrade_units(service,row):
    result=[];cache={}
    for index,drama in enumerate(row['data'].get('dramas',[])):
        key=(drama.get('app_id',''),drama.get('content_id',''));resource={}
        try:
            if key not in cache:cache[key]=service.catalog.source_resource(*key) if service.catalog else {}
            resource=cache[key]
        except VideoEditingError:
            pass
        uid='unit_'+hashlib.sha256((row['id']+':'+str(index)).encode()).hexdigest()[:24]
        result.append({'id':uid,'series_code':resource.get('series_code',''),
                       'title':drama.get('title',''),'clips':copy.deepcopy(drama.get('clips',[]))})
    return result


def source_stamp(row):
    data=row['data']
    return encode({'kind':row['kind'],'status':row['status'],'owner':row['owner'],
                   **{key:data.get(key) for key in ('identity','metadata','url','series_code','library_kind','app_id','content_id')}})


def compile_unit(service,unit,actor):
    from .recipe import validate_recipe
    code=unit.get('series_code','')
    require(code,'resource_required','请选择目标资源',409)
    clips=unit.get('clips',[])
    require(clips,'timeline_empty','请添加至少一个片段',409)
    sources=[];stamps={};attribution=[];title='';variants={};products=set()
    for sid in dict.fromkeys(c['source_id'] for c in clips):
        row=service.source(sid,actor,ready=True,check_product=False);data=row['data']
        actual=data.get('series_code','')
        if row['kind']=='episode':
            require(service.catalog is not None,'catalog_unavailable','剧目目录暂不可用',503)
            key=(data.get('app_id'),data.get('content_id'))
            if key[0] not in products:
                service.catalog.require_product(key[0]);products.add(key[0])
            if key not in variants:variants[key]=service.catalog.source_resource(*key)
            variant=variants[key]
            actual=variant['series_code']
            attribution.append({'source_id':sid,'series_code':actual,'app_id':data.get('app_id'),'content_id':data.get('content_id')})
        else:
            attribution.append({'source_id':sid,'series_code':actual,'library_kind':data.get('library_kind','material')})
            title=title or data.get('english_title','')
        require(actual,'source_resource_required','素材待补资源标签',409)
        require(actual==code,'clip_resource_mismatch','素材与目标资源不匹配',409)
        stamps[sid]=source_stamp(row)
        sources.append({'source_id':sid,'kind':row['kind'],'url':data['url'],'identity':data['identity'],**data['metadata']})
    recipe=validate_recipe({'version':2 if any(c.get('mirror') for c in clips) else 1,
                            'profile':service.export_profile,'sources':sources,'clips':clips},operation='render')
    return {'recipe':recipe,'stamps':stamps,'attribution':attribution,'english_title':title}


def batch_result(service,batch,actor):
    tasks=[service.task(tid,actor) for tid in batch['data']['task_ids']]
    return {'batch_id':batch['id'],'project_version_id':batch['data']['project_version_id'],
            'items':[service.task_dto(task) for task in tasks],
            'results':copy.deepcopy(batch['data'].get('results',[])),
            'issues':copy.deepcopy(batch['data'].get('issues',[]))}


def render_units(service,pid,payload,actor):
    from .service import operation_id
    op=operation_id(payload.get('operation_id'));expected=payload.get('expected_version')
    require(type(expected) is int and expected>=1,'project_version_conflict','项目版本无效',409)
    project=service.project(pid,actor);actor_owner=owner(actor)
    version_id='vv_'+hashlib.sha256((pid+':'+str(expected)).encode()).hexdigest()[:32]
    saved=service.store.get('project_version',version_id)
    draft=saved['data'] if saved else project['data']
    units={unit['id']:unit for unit in draft.get('units',[])}
    selected=payload.get('unit_ids',list(units))
    require(isinstance(selected,list) and 1<=len(selected)<=20 and all(isinstance(x,str) for x in selected)
            and len(set(selected))==len(selected),'unit_ids_invalid','请选择制作单元')
    selection=sorted(selected)
    with closing(service.store.connect()) as db:
        previous=db.execute('SELECT * FROM ve_render_operation WHERE owner=? AND operation_id=?',(actor_owner,op)).fetchone()
        if previous:
            batch=service.store.get('batch',previous['batch_id'],db)
            require(previous['project_id']==pid and previous['project_version']==expected and batch['data'].get('unit_ids')==selection,
                    'operation_conflict','同一制作操作内容已变化',409)
            return batch_result(service,batch,actor)
        accepted={row['unit_id']:row['task_id'] for row in db.execute(
            'SELECT unit_id,task_id FROM ve_unit_render WHERE project_id=? AND project_version=?',(pid,expected))}
    require(all(uid in units for uid in selected),'unit_not_found','所选制作单元不存在',404)
    require(project['version']==expected or all(uid in accepted for uid in selected),
            'project_version_conflict','项目已更新，请刷新后制作',409)
    compiled={};problems={}
    for uid in selected:
        if uid in accepted:continue
        try:compiled[uid]=compile_unit(service,units[uid],actor)
        except (VideoEditingError,DramaSynthesisError) as exc:
            problems[uid]=issue(units[uid],exc.code,exc.message if isinstance(exc,VideoEditingError) else str(exc))
    with service.store.transaction() as db:
        # Recheck inside one transaction after remote reads; no catalog I/O holds this lock.
        previous=db.execute('SELECT * FROM ve_render_operation WHERE owner=? AND operation_id=?',(actor_owner,op)).fetchone()
        if previous:
            batch=service.store.get('batch',previous['batch_id'],db)
            require(previous['project_id']==pid and previous['project_version']==expected and batch['data'].get('unit_ids')==selection,
                    'operation_conflict','同一制作操作内容已变化',409)
            return batch_result(service,batch,actor)
        current=service.project(pid,actor,db)
        accepted={row['unit_id']:row['task_id'] for row in db.execute(
            'SELECT unit_id,task_id FROM ve_unit_render WHERE project_id=? AND project_version=?',(pid,expected))}
        require(current['version']==expected or all(uid in accepted for uid in selected),
                'project_version_conflict','项目已更新，请刷新后制作',409)
        batch_id=identifier('vb_');task_ids=[];results=[];issues=[]
        snapshot=service.store.get('project_version',version_id,db)
        if snapshot:require(encode(snapshot['data'])==encode(draft),'project_snapshot_conflict','项目版本快照不一致',409)
        else:service.store.insert('project_version',{'id':version_id,'owner':project['owner'],'project_id':pid,'version':expected,'data':draft},db)
        for uid in selected:
            if uid in accepted:
                task=service.store.get('task',accepted[uid],db)
                require(task and can_manage(task,actor),'task_not_found','制作任务不可访问',404)
                task_ids.append(task['id']);results.append({'unit_id':uid,'status':'reused','task_id':task['id']});continue
            problem=problems.get(uid);candidate=compiled.get(uid)
            if candidate:
                for sid,stamp in candidate['stamps'].items():
                    source=service.store.get('source',sid,db)
                    if not source or source_stamp(source)!=stamp:
                        problem=issue(units[uid],'source_changed','素材状态或资源标签已变化，请重试此单元',sid);break
                if not service.intake_enabled:problem=issue(units[uid],'video_editing_intake_disabled','新剪辑任务已暂停，草稿已保留')
            if problem:
                issues.append(problem);results.append({**problem,'status':'invalid'});continue
            unit=units[uid];tid=identifier('ve_');output_id='vo_'+tid[3:]
            data={'schema_version':2,'unit_id':uid,'series_code':unit['series_code'],'english_title':candidate['english_title'],
                  'project_version':expected,'project_version_id':version_id,'batch_id':batch_id,'output_id':output_id,
                  'submitted_by':actor_owner,'request_operation_id':op,'drama_title':unit['title'] or candidate['english_title'] or unit['series_code'],
                  'source_attribution':candidate['attribution'],'stage':'queued','progress':0,
                  'payload':{'job_id':tid,'task_type':'video_editing_v1','operation':'render','recipe':candidate['recipe']}}
            task_op='unit_'+hashlib.sha256((pid+'\0'+str(expected)+'\0'+uid).encode()).hexdigest()
            service.store.insert('task',{'id':tid,'owner':project['owner'],'project_id':pid,'operation_id':task_op,
                                        'kind':'render','status':'queued','data':data},db)
            db.execute('INSERT INTO ve_unit_render(project_id,project_version,unit_id,task_id) VALUES(?,?,?,?)',(pid,expected,uid,tid))
            service.store.insert('output',{'id':output_id,'owner':project['owner'],'task_id':tid,'batch_id':batch_id,'status':'pending','data':{}},db)
            task_ids.append(tid);results.append({'unit_id':uid,'status':'accepted','task_id':tid})
        service.store.insert('batch',{'id':batch_id,'owner':project['owner'],'project_id':pid,
            'operation_id':'submit_'+hashlib.sha256((actor_owner+'\0'+op).encode()).hexdigest(),
            'data':{'schema_version':2,'project_version':expected,'project_version_id':version_id,'task_ids':task_ids,
                    'unit_ids':selection,'results':results,'issues':issues,'submitted_by':actor_owner,'request_operation_id':op}},db)
        batch=service.store.get('batch',batch_id,db)
        service._remember_render_operation(db,actor_owner,op,pid,expected,batch)
    return batch_result(service,batch,actor)
