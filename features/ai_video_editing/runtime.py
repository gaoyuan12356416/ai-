"""Private uploads, editable timelines and durable rendering for the AI editor."""
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

PREFIX = '/api/ai-video-editing'
MAX_BYTES = 1024 ** 3
ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


class Fault(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def check(ok, code, message, status=400):
    if not ok:
        raise Fault(code, message, status)


def owner(actor):
    check(actor.get('user_id'), 'login_required', '请先登录', 401)
    return json.dumps([actor.get('tenant_key', ''), actor['user_id']], separators=(',', ':'))


def ident(prefix):
    return prefix + '_' + uuid.uuid4().hex


def packed(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def probe(path):
    try:
        p = subprocess.run([os.getenv('AI_VIDEO_EDITING_FFPROBE','ffprobe'), '-v','error','-protocol_whitelist','file',
                            '-f','mov','-show_streams','-show_format','-of','json',str(path)],
                           capture_output=True, timeout=90, check=True)
        data = json.loads(p.stdout)
        v = next(s for s in data['streams'] if s['codec_type']=='video' and not s.get('disposition',{}).get('attached_pic'))
        seconds = float(v.get('duration') or data['format']['duration'])
        width, height = int(v['width']), int(v['height'])
        if not (math.isfinite(seconds) and 0 < seconds <= 86400 and 1 <= width <= 16384 and 1 <= height <= 16384):
            raise ValueError()
        return {'duration_ms': round(seconds*1000),'width':width,'height':height,
                'has_audio':any(s['codec_type']=='audio' for s in data['streams'])}
    except (subprocess.SubprocessError, ValueError, KeyError, StopIteration, TypeError):
        raise Fault('invalid_video','无法读取视频。请选择含有效画面的 MP4 或 MOV 文件。',422) from None


class Runtime:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.db_path = str(self.root/'editor.sqlite3')
        for sub in ('uploads','outputs','temp'):
            (self.root/sub).mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript("""
            CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,
                series TEXT NOT NULL,episode INTEGER NOT NULL,sha256 TEXT NOT NULL,meta TEXT NOT NULL,created REAL NOT NULL,
                UNIQUE(owner,name,series,episode,sha256));
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,
                units TEXT NOT NULL,version INTEGER NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,owner TEXT NOT NULL,project TEXT NOT NULL,
                version INTEGER NOT NULL,unit_id TEXT NOT NULL,unit_data TEXT NOT NULL,status TEXT NOT NULL,
                lease TEXT NOT NULL DEFAULT '',expires REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',meta TEXT NOT NULL DEFAULT '{}',created REAL NOT NULL,
                UNIQUE(project,version,unit_id));
            CREATE TABLE IF NOT EXISTS operations(owner TEXT,kind TEXT,op TEXT,digest TEXT NOT NULL,
                receipt TEXT NOT NULL,PRIMARY KEY(owner,kind,op));
            CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(status,created);
            CREATE INDEX IF NOT EXISTS assets_owner ON assets(owner,created);
            CREATE INDEX IF NOT EXISTS projects_owner ON projects(owner,updated);
            """)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.db_path,timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if write: db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def asset_info(self,row):
        check(row is not None,'asset_missing','素材不存在或没有访问权限',404)
        rid = row['id']
        meta = json.loads(row['meta'])
        return {'id':rid,'name':row['name'],'series_code':row['series'],'episode_number':row['episode'] or None,'status':'ready','metadata':meta,
                **meta,'sha256':row['sha256'],'path':str(self.root/'uploads'/(rid+'.mp4')),
                'preview_url':f'{PREFIX}/assets/{rid}/media'}

    def asset(self, rid, actor):
        with self.db() as db:
            return self.asset_info(db.execute('SELECT * FROM assets WHERE id=? AND owner=?',(rid,owner(actor))).fetchone())

    def import_asset(self,path,digest,name,series,actor,episode=0):
        meta=probe(path)
        key=owner(actor)
        with self.db(True) as db:
            args=(key,name,series,episode,digest)
            row=db.execute('SELECT * FROM assets WHERE owner=? AND name=? AND series=? AND episode=? AND sha256=?',args).fetchone()
            if not row:
                rid=ident('aa')
                dest=self.root/'uploads'/(rid+'.mp4')
                os.replace(path,dest)
                try:
                    db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)',(rid,*args,packed(meta),time.time()))
                    db.commit()
                except BaseException:
                    dest.unlink(missing_ok=True)
                    raise
                row=db.execute('SELECT * FROM assets WHERE id=?',(rid,)).fetchone()
            return self.asset_info(row)

    def assets(self,actor,q,limit,offset,series=''):
        args=(owner(actor),q,q)
        cond='owner=? AND (instr(lower(name),lower(?))>0 OR instr(lower(series),lower(?))>0)'
        if series:
            cond+=' AND series=?'
            args+= (series,)
        with self.db() as db:
            total=db.execute('SELECT count(*) FROM assets WHERE '+cond,args).fetchone()[0]
            rows=db.execute('SELECT * FROM assets WHERE '+cond+' ORDER BY created DESC LIMIT ? OFFSET ?',(*args,limit,offset))
            return {'items':[public_asset(self.asset_info(r)) for r in rows],'total':total}

    def series(self,actor):
        with self.db() as db:
            return {'items':[dict(r) for r in db.execute(
                "SELECT series AS series_code,count(*) AS asset_count FROM assets WHERE owner=? AND series<>'' GROUP BY series ORDER BY max(created) DESC",(owner(actor),))]}

    def normalize(self,units,actor):
        check(isinstance(units,list) and 1<=len(units)<=100,'units_invalid','请提供 1–100 个制作单元')
        result, seen, assets = [], set(), {}
        for i,u in enumerate(units):
            check(isinstance(u,dict),'unit_invalid','制作单元无效')
            uid=u.get('id','')
            check(isinstance(uid,str) and ID.fullmatch(uid) and uid not in seen,'unit_invalid','制作单元编号重复或无效')
            seen.add(uid)
            clips=u.get('clips')
            check(isinstance(clips,list) and 1<=len(clips)<=300,'clip_count','请保留 1–300 个片段')
            clean=[]
            for j,c in enumerate(clips):
                check(isinstance(c,dict),'clip_invalid','片段无效')
                sid=c.get('source_id')
                check(isinstance(sid,str),'asset_missing','片段缺少素材')
                if sid not in assets: assets[sid]=self.asset(sid,actor)
                start,end=c.get('start_ms'),c.get('end_ms')
                check(type(start) is int and type(end) is int and 0<=start<end<=assets[sid]['duration_ms']
                      and (end*30+500)//1000>(start*30+500)//1000,'clip_range','片段超出素材范围或短于一帧')
                check(c.get('fit','contain') in ('contain','cover') and c.get('transition','cut')=='cut' and
                      all(type(c.get(k,False)) is bool for k in ('mirror','mute')),'clip_style','不支持的片段样式')
                zoom=c.get('zoom_percent',100)
                check(type(zoom) is int and 100<=zoom<=130,'clip_zoom','缩放须在 100% 到 130% 之间')
                clean.append({'id':str(c.get('id') or 'clip_'+str(j))[:128],'source_id':sid,'start_ms':start,'end_ms':end,
                              'fit':c.get('fit','contain'),'mirror':c.get('mirror',False),'mute':c.get('mute',False),'transition':'cut',
                              'zoom_percent':zoom})
            item={'id':uid,'title':str(u.get('title') or '作品 '+str(i+1))[:200],
                  'series_code':str(u.get('series_code',''))[:180],'clips':clean}
            if u.get('composition') is not None:
                from .composition import normalize, audio
                item['composition']=normalize(u['composition'],sum(c['end_ms']-c['start_ms'] for c in clean))
                if item['composition']['speech_id']:
                    audio(self,item['composition']['speech_id'],owner(actor))
            result.append(item)
        return result

    def receipt(self,actor,kind,payload,fn):
        op=payload.get('operation_id','')
        check(isinstance(op,str) and ID.fullmatch(op),'operation_required','请提供本次操作编号')
        key=owner(actor)
        digest=hashlib.sha256(packed(payload).encode()).hexdigest()
        with self.db(True) as db:
            row=db.execute('SELECT * FROM operations WHERE owner=? AND kind=? AND op=?',(key,kind,op)).fetchone()
            if row:
                check(row['digest']==digest,'operation_conflict','操作编号已用于另一份配置',409)
                return json.loads(row['receipt'])
            result=fn(db,key)
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',(key,kind,op,digest,packed(result)))
            return result

    @staticmethod
    def project_info(row):
        check(row is not None,'project_missing','剪辑草稿不存在或没有访问权限',404)
        return {'id':row['id'],'name':row['name'],'version':row['version'],'units':json.loads(row['units']),'updated_at':row['updated']}

    def save(self, actor, body, pid=None):
        units=self.normalize(body.get('units'),actor)
        name=body.get('name')
        check(isinstance(name,str) and 0<len(name.strip())<=180,'name_invalid','请填写不超过 180 字的草稿名')
        def write(db,key):
            rid=pid or ident('ap')
            if pid:
                row=db.execute('SELECT version FROM projects WHERE id=? AND owner=?',(pid,key)).fetchone()
                check(row,'project_missing','草稿不存在或没有访问权限',404)
                check(row['version']==body.get('expected_version'),'version_conflict','此草稿已更新，请刷新后再保存',409)
                db.execute('UPDATE projects SET units=?,name=?,version=version+1,updated=? WHERE id=?',(packed(units),name.strip(),time.time(),rid))
            else:
                db.execute('INSERT INTO projects VALUES(?,?,?,?,1,?)',(rid,key,name.strip(),packed(units),time.time()))
            return {'item':self.project_info(db.execute('SELECT * FROM projects WHERE id=?',(rid,)).fetchone())}
        return self.receipt(actor,'save:'+str(pid or ''),body,write)

    def render(self, pid,actor,body):
        def write(db,key):
            p=self.project_info(db.execute('SELECT * FROM projects WHERE id=? AND owner=?',(pid,key)).fetchone())
            check(body.get('expected_version')==p['version'],'version_conflict','草稿已更新，请刷新后再制作',409)
            units={u['id']:u for u in p['units']}
            chosen=body.get('unit_ids',list(units))
            check(isinstance(chosen,list) and 0<len(chosen)<=len(units) and all(isinstance(i,str) and i in units for i in chosen)
                  and len(set(chosen))==len(chosen),'selection_invalid','所选制作单元无效')
            rows=[]
            for uid in chosen:
                ref=(pid,p['version'],uid)
                row=db.execute('SELECT * FROM tasks WHERE project=? AND version=? AND unit_id=?',ref).fetchone()
                if not row:
                    tid=ident('at')
                    db.execute('INSERT INTO tasks(id,owner,project,version,unit_id,unit_data,status,created) VALUES(?,?,?,?,?,?,?,?)',
                               (tid,key,*ref,packed(units[uid]),'queued',time.time()))
                    row=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
                rows.append(self.task_info(row))
            return {'items':rows,'issues':[]}
        return self.receipt(actor,'render:'+pid,body,write)

    @staticmethod
    def task_info(row):
        check(row is not None,'task_missing','作品不存在或没有访问权限',404)
        name=json.loads(row['unit_data'])['title']
        rid=row['id']
        result={'id':rid,'project_id':row['project'],'name':name,'drama_title':name,'status':row['status'],
                'unit_id':row['unit_id'],'created_at':row['created'],'error_message':row['error']}
        if row['status']=='completed':
            uri=f'{PREFIX}/tasks/{rid}/media'
            result['output']={'preview_url':uri,'download_url':uri+'?download=1','metadata':json.loads(row['meta'])}
        return result

    def list_records(self,kind,actor,rid=None):
        table,fn,order={'tasks':('tasks',self.task_info,'created'),'projects':('projects',self.project_info,'updated')}[kind]
        with self.db() as db:
            if rid: return {'item':fn(db.execute(f'SELECT * FROM {table} WHERE owner=? AND id=?',(owner(actor),rid)).fetchone())}
            rows=db.execute(f'SELECT * FROM {table} WHERE owner=? ORDER BY {order} DESC LIMIT 150',(owner(actor),))
            return {'items':[fn(row) for row in rows]}

    def claim(self):
        now=time.time()
        with self.db(True) as db:
            db.execute("UPDATE tasks SET status='failed', error='制作节点多次中断，可重新制作' WHERE status='running' AND expires<? AND attempts>=4",(now,))
            row=db.execute("SELECT * FROM tasks WHERE status='queued' OR (status='running' AND expires<?) ORDER BY created LIMIT 1",(now,)).fetchone()
            if not row:return None
            lease=uuid.uuid4().hex
            db.execute("UPDATE tasks SET status='running',lease=?,expires=?,attempts=attempts+1 WHERE id=?",(lease,now+180,row['id']))
            return {'id':row['id'],'lease':lease,'unit':json.loads(row['unit_data'])}

    def retry(self,actor,tid):
        with self.db(True) as db:
            row=db.execute('SELECT * FROM tasks WHERE id=? AND owner=?',(tid,owner(actor))).fetchone()
            self.task_info(row)
            if row['status']=='failed':
                db.execute("UPDATE tasks SET status='queued',error='',attempts=0,expires=0,lease='' WHERE id=?",(tid,))
            return {'item':self.task_info(db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())}

    def leased(self,db,tid,lease):
        row=db.execute("SELECT * FROM tasks WHERE id=? AND status='running' AND lease=?",(tid,lease)).fetchone()
        check(row,'lease_lost','制作已由其他节点接管',409)
        return row

    def worker_asset(self,tid,lease,sid):
        with self.db() as db:
            job=self.leased(db,tid,lease)
            check(any(c['source_id']==sid for c in json.loads(job['unit_data'])['clips']),'asset_missing','素材不属于此任务',404)
            return self.asset_info(db.execute('SELECT * FROM assets WHERE owner=? AND id=?',(job['owner'],sid)).fetchone())

    def completion_receipt(self,tid,lease,digest):
        with self.db() as db:
            row=db.execute("SELECT lease,meta FROM tasks WHERE id=? AND status='completed'",(tid,)).fetchone()
            if not row: return False
            check(lease and lease==row['lease'] and json.loads(row['meta']).get('sha256')==digest,
                  'lease_lost','这项制作已有不同回执',409)
            return True

    def update_job(self,tid,lease,action,path=None,digest=''):
        meta=probe(path) if path else None
        with self.db(True) as db:
            if action=='complete':
                done=db.execute("SELECT lease,meta FROM tasks WHERE id=? AND status='completed'",(tid,)).fetchone()
                if done:
                    check(done['lease']==lease and json.loads(done['meta']).get('sha256')==digest,'lease_lost','这项制作已有不同回执',409)
                    return {'ok':True}
            job=self.leased(db,tid,lease)
            if action=='complete':
                clips=json.loads(job['unit_data'])['clips']
                frames=sum((c['end_ms']*30+500)//1000-(c['start_ms']*30+500)//1000 for c in clips)
                check(meta['width']==720 and meta['height']==1280 and meta['has_audio'] and abs(meta['duration_ms']-frames*1000/30)<150,
                      'output_mismatch','成片尺寸、时长或音轨校验失败',422)
                dest=self.root/'outputs'/(tid+'.mp4')
                os.replace(path,dest)
                meta['sha256']=digest
                db.execute("UPDATE tasks SET status='completed',meta=? WHERE id=?",(packed(meta),tid))
            elif action=='fail':
                db.execute("UPDATE tasks SET status='failed',lease='',error='此素材制作失败，可重试或调整时间线' WHERE id=?",(tid,))
            else: db.execute('UPDATE tasks SET expires=? WHERE id=?',(time.time()+180,tid))
        return {'ok':True}


def public_asset(asset):
    return {k:v for k,v in asset.items() if k not in ('path','sha256')}
