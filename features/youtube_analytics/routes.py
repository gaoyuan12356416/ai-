"""Authenticated, read-only report API with bounded shared source caching."""
import json
import os
import subprocess
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import parse_qs

from .report import ReportError, csv_export, options, parameters, read_catalog, summarize

_cache = OrderedDict()
_lock = threading.Lock()
TTL = 300
MAX_ROWS = 50000


def metric_sql(start, end):
    # Both values have been normalized by date.fromisoformat before reaching here.
    return """SELECT HEX(JSON_OBJECT('date',CAST(dt AS CHAR),
      'campaign_id',MIN(campaign_id),'campaign',MIN(campaign),
      'clicks',SUM(COALESCE(clicks,0)),'views',SUM(COALESCE(views,0)),
      'installs',SUM(COALESCE(installs,0)),'conversions',SUM(COALESCE(recharge,0)),
      'revenue_cents',CAST(ROUND(SUM(COALESCE(revenue,0))*100) AS SIGNED),
      'refund_cents',CAST(ROUND(SUM(COALESCE(refund_revenue,0))*100) AS SIGNED),
      'updated_at',CAST(MAX(updated_at) AS CHAR)))
      FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(sd)
      WHERE site_id='2284' AND dt BETWEEN '%s' AND '%s'
      GROUP BY dt,BINARY campaign_id,BINARY campaign LIMIT %d""" % (start,end,MAX_ROWS+1)


def query_metrics(app, start, end):
    if (str(app['ADMIN_MAPPING_MYSQL_HOST'])!='101.32.56.53' or str(app['ADMIN_MAPPING_MYSQL_PORT'])!='63350'):
        raise ReportError('报表只读数据源配置异常',503)
    env = dict(os.environ)
    env.pop('SQL_GATE_BYPASS',None)
    env.update(MYSQL_PWD=app['ADMIN_MAPPING_MYSQL_PASSWORD'],SQL_GATE_JOB_NAME='youtube-analytics',
               SQL_GATE_WAIT_TIMEOUT_SECONDS='5',SQL_GATE_EXEC_TIMEOUT_SECONDS='12')
    sql = "SET SESSION time_zone='+00:00'; SET SESSION MAX_EXECUTION_TIME=8000; START TRANSACTION READ ONLY; SELECT @@read_only; "+metric_sql(start,end)+'; ROLLBACK;'
    try:
        result=subprocess.run(['/usr/bin/mysql','-h','101.32.56.53','-P','63350','-u',str(app['ADMIN_MAPPING_MYSQL_USER']),
            '--connect-timeout=3','--default-character-set=utf8mb4','--skip-reconnect','-N','-B','-r'],
            input=sql,env=env,text=True,encoding='utf-8',capture_output=True,timeout=25)
        lines=result.stdout.splitlines()
        if result.returncode or not lines or lines.pop(0)!='1':
            raise ValueError('readonly_query_failed')
        rows=[json.loads(bytes.fromhex(line).decode('utf-8')) for line in lines if line.strip()]
        if len(rows)>MAX_ROWS:
            raise ReportError('数据量超过在线查询上限，请缩小日期范围',400)
        return rows
    except ReportError:
        raise
    except Exception:
        # Driver messages can contain connection metadata. Never return them.
        raise ReportError('效果数据读取失败或繁忙，请稍后重试；本次不按零统计',503) from None


def source(app, start, end):
    key=(start,end)
    if not _lock.acquire(timeout=1):
        raise ReportError('报表正在更新，请稍后重试',503)
    try:
        cached=_cache.get(key)
        if cached and time.monotonic()-cached[0]<TTL:
            _cache.move_to_end(key)
            return cached[1],cached[2]
        rows=query_metrics(app,start,end)
        fetched=datetime.now(timezone.utc).isoformat()
        _cache[key]=(time.monotonic(),rows,fetched)
        _cache.move_to_end(key)
        while len(_cache)>8:
            _cache.popitem(last=False)
        return rows,fetched
    finally:
        _lock.release()


def dispatch(handler, parsed, app):
    reply=lambda status,value:app['json_response'](handler,status,value,no_store=True)
    actor=handler._youtube_auto_actor('youtubeAnalytics')
    if actor is None:
        return
    if handler.command!='GET' or parsed.path not in ('/api/youtube-analytics/options','/api/youtube-analytics/report','/api/youtube-analytics/export.csv'):
        handler.close_connection=True
        return reply(404,dict(error='not_found'))
    try:
        query=parse_qs(parsed.query,keep_blank_values=True,max_num_fields=520)
        if parsed.path.endswith('/options'):
            if query:
                raise ReportError('筛选选项接口不接受参数')
            return reply(200,options(read_catalog(app['JOB_DB_PATH']),actor))
        params=parameters(query)
        catalog=read_catalog(app['JOB_DB_PATH'])
        metrics,fetched=source(app,params['start'],params['end'])
        report=summarize(catalog,metrics,actor,params,fetched)
        if parsed.path.endswith('/export.csv'):
            data=csv_export(report)
            handler.send_response(200)
            for k,v in {'Content-Type':'text/csv; charset=utf-8','Content-Length':str(len(data)),
                        'Content-Disposition':'attachment; filename="youtube-analytics-%s-%s.csv"'%(params['start'],params['end']),
                        'Cache-Control':'no-store','Vary':'Cookie','X-Content-Type-Options':'nosniff'}.items():
                handler.send_header(k,v)
            handler.end_headers();handler.wfile.write(data)
            return
        report.pop('all_rows')
        return reply(200,report)
    except ReportError as exc:
        return reply(exc.status,dict(error='report_unavailable' if exc.status==503 else 'invalid_request',message=str(exc)))
    except ValueError:
        return reply(400,dict(error='invalid_request',message='请求参数无效'))
    except Exception:
        return reply(503,dict(error='report_unavailable',message='报表暂不可用，请稍后重试；本次不按零统计'))
