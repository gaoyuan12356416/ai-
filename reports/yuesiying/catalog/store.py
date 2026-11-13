"""Immutable material catalog generations; all verification uses the read replica."""
import contextlib
import datetime as dt
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import subprocess
import time

FIELDS = {'id', 'name', 'language', 'data_source_id', 'video_duration', 'created_at'}
GEN = 'ads_ai.report_material_catalog_generation'
ROWS = 'ads_ai.report_material_catalog_row'
POINTER = 'ads_ai.report_material_catalog_pointer'
PROJECTION = 'source_ordinal,material_id,content_id,language,name,video_duration,source_created_at'


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def normalize(row):
    require(set(row) == FIELDS, 'catalog_source_fields_changed')
    require(isinstance(row['name'], str) and isinstance(row['language'], str), 'invalid_name_or_language')
    material = int(row['id'])
    duration = Decimal(str(row['video_duration'] or 0))
    require(material > 0 and duration.is_finite(), 'invalid_material_or_duration')
    # These are the exact conversions made by the existing localization builder.
    result = {'id': material, 'data_source_id': str(row['data_source_id']),
              'name': row['name'], 'language': row['language'],
              'video_duration': format(duration.normalize(), 'f'), 'created_at': str(row['created_at'])}
    require(len(result['data_source_id']) <= 128 and len(result['language']) <= 32
            and len(result['created_at']) <= 40 and len(result['video_duration']) <= 64, 'catalog_field_too_long')
    return result


def iter_json_array(path, chunk_size=1024 * 1024):
    """Stream JSON with an offset cursor; do not recopy a megabyte per row."""
    decoder=json.JSONDecoder()
    with Path(path).open('r',encoding='utf-8-sig') as stream:
        buffer='';position=0;ended=False;mode='start'
        while True:
            while position<len(buffer) and buffer[position].isspace():position+=1
            if position==len(buffer):
                require(not ended,'incomplete_catalog_json')
                buffer=stream.read(chunk_size);position=0;ended=not buffer
                continue
            char=buffer[position]
            if mode=='start':
                require(char=='[','catalog_array_required');position+=1;mode='first';continue
            if char==']':
                require(mode in ('first','separator'),'trailing_catalog_separator')
                require(not buffer[position+1:].strip(),'trailing_catalog_data')
                for piece in iter(lambda:stream.read(chunk_size),''):
                    require(not piece.strip(),'trailing_catalog_data')
                return
            if mode=='separator':
                require(char==',','catalog_separator_required');position+=1;mode='value';continue
            try:value,offset=decoder.raw_decode(buffer,position)
            except json.JSONDecodeError:
                require(not ended,'incomplete_catalog_json')
                piece=stream.read(chunk_size);ended=not piece
                buffer=buffer[position:]+piece;position=0;continue
            require(isinstance(value,dict),'catalog_row_object_required')
            yield value
            position=offset;mode='separator'


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def connect(config, writer=False, streaming=False, source=False):
    settings = json.loads(Path(config).read_text())['mysql']
    require(settings['host'] == '101.32.56.53', 'unexpected_database_host')
    if not writer:
        import pymysql
        connection=pymysql.connect(host=settings['host'],port=63350,user=settings['user'],password=settings['password'],
            database='kunlunads_dev' if source else 'ads_ai',charset='utf8mb4',connect_timeout=5,
            read_timeout=60,write_timeout=60,cursorclass=pymysql.cursors.DictCursor,autocommit=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT DATABASE() db,@@read_only ro');identity=cursor.fetchone()
            require(identity['db']==('kunlunads_dev' if source else 'ads_ai') and int(identity['ro'])==1,'database_role_mismatch')
            return connection
        except BaseException:connection.close();raise
    import mysql.connector
    raw = mysql.connector.connect(host=settings['host'], port=63353 if writer else 63350,
        user=settings['user'], password=settings['password'], database='kunlunads_dev' if source else 'ads_ai',
        charset='utf8mb4', connection_timeout=5, read_timeout=60, write_timeout=60,
        compress=True, use_pure=True, autocommit=False)
    class Connection:
        def cursor(self):return raw.cursor(dictionary=True,buffered=not streaming)
        def commit(self):return raw.commit()
        def rollback(self):return raw.rollback()
        def close(self):return raw.close()
        def __enter__(self):return self
        def __exit__(self,*args):self.close()
    connection=Connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT DATABASE() db,@@read_only ro')
            identity = cursor.fetchone()
            cursor.fetchall()
        require(identity['db'] == ('kunlunads_dev' if source else 'ads_ai')
                and int(identity['ro']) == (0 if writer else 1), 'database_role_mismatch')
        return connection
    except BaseException:
        connection.close(); raise


def metadata(config, generation):
    require(re.fullmatch('[0-9a-f]{64}', generation or ''), 'invalid_catalog_generation')
    with contextlib.closing(connect(config)) as db, db.cursor() as cursor:
        cursor.execute('SELECT * FROM '+GEN+' WHERE generation_id=%s', (generation,))
        return cursor.fetchone()


def _row(record):
    return {'id': int(record['material_id']), 'data_source_id': record['content_id'],
            'name': record['name'], 'language': record['language'],
            'video_duration': record['video_duration'], 'created_at': record['source_created_at']}


class NativeReadError(RuntimeError):
    def __init__(self,exit_code,codes):
        self.retryable=bool(set(codes)&{'2003','2006','2013'})
        super().__init__('catalog_native_read_failed:exit='+str(exit_code)+',codes='+','.join(codes))


def native_json_rows(config,query):
    for attempt in range(3):
        try:
            # Collect the complete page before exposing any rows. A transport
            # retry cannot duplicate a partially yielded page.
            page=list(_native_json_rows_once(config,query))
        except (NativeReadError,subprocess.TimeoutExpired) as error:
            if attempt==2 or isinstance(error,NativeReadError) and not error.retryable:raise
            print(json.dumps({'phase':'read_transport_retry','attempt':attempt+1,
                'error_type':type(error).__name__}),file=sys.stderr,flush=True)
            time.sleep(attempt+1)
            continue
        yield from page
        return


def _native_json_rows_once(config,query):
    """Native compressed protocol avoids the pure Python driver's large-packet bug."""
    settings=json.loads(Path(config).read_text())['mysql']
    require(settings['host']=='101.32.56.53','unexpected_database_host')
    env=dict(os.environ,MYSQL_PWD=settings['password'])
    reader='/usr/bin/mysql.real' if os.environ.get('REPORT_MATERIAL_CATALOG_MANUAL_READ')=='1' else '/usr/bin/mysql'
    command=[reader,'--compress','--connect-timeout=5','--default-character-set=utf8mb4',
        '--batch','--raw','--skip-column-names','-h',settings['host'],'-P','63350','-u',settings['user'],'-D','ads_ai']
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        env=env,text=True,encoding='utf-8',start_new_session=True)
    try:
        output,error=process.communicate('SELECT @@read_only; SET SESSION MAX_EXECUTION_TIME=60000; '+query+';\n',timeout=90)
        codes=re.findall(r'\bERROR\s+(\d+)\b',error)
        if process.returncode!=0:raise NativeReadError(process.returncode,codes)
        lines=output.splitlines();require(lines and lines[0].strip()=='1','database_role_mismatch')
        for line in lines[1:]:
            if line.strip():yield json.loads(line)
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.communicate()


def catalog_pages(config,generation,content_ids=None):
    require(re.fullmatch('[0-9a-f]{64}',generation or ''),'invalid_catalog_generation')
    columns=PROJECTION.split(',')
    projection='JSON_OBJECT('+','.join("'"+name+"',"+name for name in columns)+')'
    clause="generation_id='"+generation+"'"
    if content_ids is not None:
        if not content_ids:return
        clause+=' AND content_id IN ('+','.join("CONVERT(0x"+value.encode().hex()+" USING utf8mb4)" if value else "''" for value in content_ids)+')'
    after=0
    while True:
        count=0
        query='SELECT '+projection+' FROM '+ROWS+' WHERE '+clause+' AND source_ordinal>'+str(after)+' ORDER BY source_ordinal LIMIT 25000'
        for record in native_json_rows(config,query):
            ordinal=int(record['source_ordinal']);require(ordinal>after,'catalog_ordinal_not_increasing');after=ordinal;count+=1
            yield record
        if count<25000:return


def verify(config, generation, expected_count, expected_digest):
    # A bounded poll tolerates replication lag without querying the writer.
    deadline = time.monotonic() + 60
    while True:
        with contextlib.closing(connect(config)) as db, db.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) n FROM '+ROWS+' WHERE generation_id=%s', (generation,))
            count = int(cursor.fetchone()['n'])
        if count == expected_count:
            break
        require(time.monotonic() < deadline, 'catalog_replica_not_caught_up')
        time.sleep(1)
    digest = hashlib.sha256(); count = 0
    for record in catalog_pages(config,generation):
        count += 1
        require(record['source_ordinal'] == count, 'catalog_ordinal_gap')
        digest.update((canonical(_row(record))+'\n').encode())
    require(count == expected_count and digest.hexdigest() == expected_digest, 'catalog_full_readback_mismatch')
    return {'rows': count, 'row_sha256': digest.hexdigest()}


def import_rows(config, generation, source_hash, kind, generated_at, source_bytes, values):
    prior = metadata(config, generation)
    resume_count=0
    if prior:
        require(prior['source_sha256']==source_hash and prior['source_kind']==kind
                and prior['generated_at']==generated_at and int(prior['source_bytes'])==source_bytes,'catalog_source_identity_changed')
        if prior['status']=='verified':
            return {'generation_id': generation, 'rows': int(prior['row_count']), 'row_sha256': prior['row_sha256'], 'reused': True}
        require(prior['status']=='staging','catalog_state_not_resumable')
        with contextlib.closing(connect(config)) as db,db.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) n,COALESCE(MAX(source_ordinal),0) last_ordinal FROM '+ROWS+' WHERE generation_id=%s',(generation,))
            state=cursor.fetchone();resume_count=int(state['n'])
            require(resume_count==int(state['last_ordinal']),'catalog_staging_ordinal_gap')
    digest = hashlib.sha256(); count = 0; batch = []; batch_bytes=0; reported=0
    with contextlib.closing(connect(config, writer=True)) as db:
        with db.cursor() as cursor:
            if not prior:
                cursor.execute('INSERT INTO '+GEN+' (generation_id,status,source_sha256,source_kind,generated_at,source_bytes) VALUES(%s,%s,%s,%s,%s,%s)',
                    (generation,'staging',source_hash,kind,generated_at,source_bytes)); db.commit()
            for raw in values:
                row = normalize(raw); count += 1
                digest.update((canonical(row)+'\n').encode())
                if count<=resume_count:
                    if count==resume_count:
                        verify(config,generation,resume_count,digest.hexdigest())
                        print(json.dumps({'phase':'resumed_verified_prefix','rows':resume_count}),file=sys.stderr,flush=True)
                    continue
                batch.append((generation,count,row['id'],row['data_source_id'],row['language'],row['name'],row['video_duration'],row['created_at']))
                batch_bytes+=len(canonical(row).encode())+100
                if len(batch)>=5000 or batch_bytes>=1024*1024:
                    cursor.executemany('INSERT INTO '+ROWS+' VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',batch); db.commit(); batch.clear();batch_bytes=0
                    if count-reported>=10000:
                        print(json.dumps({'phase':'importing','rows':count}),file=sys.stderr,flush=True);reported=count
            if batch:
                cursor.executemany('INSERT INTO '+ROWS+' VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',batch); db.commit()
            require(count > 0, 'empty_catalog_requires_review')
            require(count>=resume_count,'catalog_source_shorter_than_staging')
        checked = verify(config,generation,count,digest.hexdigest())
        with db.cursor() as cursor:
            cursor.execute('UPDATE '+GEN+" SET status='verified',row_count=%s,row_sha256=%s,verified_at=UTC_TIMESTAMP(6) WHERE generation_id=%s AND status='staging' LIMIT 1",
                (count,digest.hexdigest(),generation)); require(cursor.rowcount==1,'catalog_state_changed'); db.commit()
    return {'generation_id':generation,**checked,'reused':False}


def import_json(config, path, generated_at):
    path = Path(path); checksum = file_sha(path)
    def stable_values():
        yield from iter_json_array(path)
        require(file_sha(path)==checksum,'catalog_source_changed_during_import')
    return import_rows(config,checksum,checksum,'legacy-json',generated_at,path.stat().st_size,stable_values())


def activate(config, report_key, generation):
    require(re.fullmatch('[A-Za-z0-9_.-]{1,80}',report_key), 'invalid_report_key')
    record = metadata(config,generation)
    require(record and record['status']=='verified','catalog_not_verified')
    with contextlib.closing(connect(config,writer=True)) as db, db.cursor() as cursor:
        cursor.execute('INSERT INTO '+POINTER+' (report_key,generation_id) VALUES(%s,%s) ON DUPLICATE KEY UPDATE '
            'previous_generation_id=IF(generation_id<>VALUES(generation_id),generation_id,previous_generation_id),'
            'generation_id=VALUES(generation_id),updated_at=UTC_TIMESTAMP(6)',(report_key,generation));db.commit()


def scoped_rows(config, generation, content_ids):
    record = metadata(config,generation)
    require(record and record['status']=='verified','catalog_not_verified')
    result=[]; ids=sorted(set(map(str,content_ids)))
    for offset in range(0,len(ids),200):
        chunk=ids[offset:offset+200];exact=set(chunk)
        # MySQL VARCHAR equality can ignore trailing spaces; the old Python
        # catalog membership test does not. Keep its exact string semantics.
        result.extend((int(r['source_ordinal']),_row(r)) for r in catalog_pages(config,generation,chunk) if r['content_id'] in exact)
        if offset%2000==0 or offset+200>=len(ids):
            print(json.dumps({'phase':'scoped_read','content_ids_completed':min(offset+200,len(ids)),
                'content_ids_total':len(ids),'rows':len(result)}),file=sys.stderr,flush=True)
    result.sort(key=lambda item:item[0])
    return [row for _,row in result]


def source_rows(config, sql):
    require(sql.lstrip().upper().startswith('SELECT ') and ';' not in sql
            and re.search(r'\bkunlunads_dev\s*\.\s*ads_custom_source\b',sql,re.I), 'approved_catalog_source_select_required')
    after=0
    with contextlib.closing(connect(config,source=True)) as db, db.cursor() as cursor:
        while True:
            cursor.execute('SELECT * FROM ('+sql+') catalog_source WHERE id>%s ORDER BY id LIMIT 2000',(after,))
            page=cursor.fetchall()
            if not page:return
            for row in page:yield row
            next_after=int(page[-1]['id']);require(next_after>after,'catalog_source_cursor_stalled');after=next_after


def refresh_source(config, sql_path, generated_at):
    sql=Path(sql_path).read_text().strip().rstrip(';')
    checksum=hashlib.sha256(sql.encode()).hexdigest()
    generation=hashlib.sha256((checksum+'\n'+generated_at).encode()).hexdigest()
    return import_rows(config,generation,checksum,'readonly-sql',generated_at,0,source_rows(config,sql))
