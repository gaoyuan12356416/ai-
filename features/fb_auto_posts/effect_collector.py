"""Bounded GET-only Meta snapshots; SQL uses the host's shared FIFO CLI gate."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from .effects import write_effect

UTC = timezone.utc
BJ = timezone(timedelta(hours=8))
METRICS = 'post_media_view,post_video_avg_time_watched,post_clicks_by_type'


class FeedbackError(RuntimeError):
    pass


def graph_created_at(value):
    """Normalize Meta's +0000 offset on Python runtimes before 3.11."""
    if not isinstance(value,str):
        raise FeedbackError('feedback_created_time_invalid')
    normalized = re.sub(r'([+-]\d{2})(\d{2})$',r'\1:\2',value.removesuffix('Z')+'+00:00' if value.endswith('Z') else value)
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            raise ValueError('timezone_required')
        return parsed.astimezone(UTC).isoformat(timespec='seconds')
    except ValueError:
        raise FeedbackError('feedback_created_time_invalid') from None


def ids_sql(values):
    items = [str(v) for v in values]
    if not items or len(items)>1000 or any(not re.fullmatch(r'[1-9][0-9]{0,30}',v) for v in items):
        raise FeedbackError('feedback_id_scope_invalid')
    return ','.join("'"+v+"'" for v in items)


class GatedReader:
    def __init__(self, env):
        self.env = env
        if env.get('FB_AUTO_MYSQL_HOST') != '101.32.56.53' or int(env.get('FB_AUTO_MYSQL_PORT','0')) != 63350:
            raise FeedbackError('feedback_read_endpoint_invalid')

    def query(self, sql):
        if not sql.lstrip().upper().startswith('SELECT') or ';' in sql:
            raise FeedbackError('feedback_readonly_sql_required')
        env = dict(os.environ)
        env.pop('SQL_GATE_BYPASS', None)
        env['MYSQL_PWD'] = self.env['FB_AUTO_MYSQL_PASSWORD']
        env['SQL_GATE_JOB_LABEL'] = 'fb-post-effects'
        command = ['/usr/bin/mysql','--protocol=TCP','-h','101.32.56.53','-P','63350',
                   '-u',self.env['FB_AUTO_MYSQL_USER'],'--connect-timeout=5',
                   '--default-character-set=utf8mb4','--batch','--raw','--skip-column-names',
                   'kunlunads_dev']
        result = subprocess.run(command,input='SELECT @@read_only; SET SESSION MAX_EXECUTION_TIME=15000; '+sql+';',
                                env=env,text=True,encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
        lines = result.stdout.splitlines()
        if result.returncode or not lines or lines.pop(0) != '1' or len(result.stdout)>16*1024*1024:
            raise FeedbackError('feedback_gated_read_failed')
        try:
            return [json.loads(line) for line in lines if line]
        except ValueError:
            raise FeedbackError('feedback_read_result_invalid') from None

    def credentials(self, page_ids):
        rows = self.query("SELECT JSON_OBJECT('page_id',page_id,'id',id,'user_id',fb_user_id,'value',page_access_token) FROM ads_facebook_page_post WHERE status<>1 AND page_access_token<>'' AND page_id IN ("+ids_sql(page_ids)+") ORDER BY updated_at DESC LIMIT 5000")
        if len(rows)>=5000:
            raise FeedbackError('feedback_credential_scope_too_large')
        preferred_id = str(self.env.get('FB_AUTO_EFFECTS_PREFERRED_CREDENTIAL_ID') or '')
        preferred_user = ''
        if preferred_id:
            preferred = self.query("SELECT JSON_OBJECT('user_id',fb_user_id) FROM ads_facebook_page_post WHERE id IN ("+ids_sql([preferred_id])+") AND status<>1 LIMIT 1")
            preferred_user = str(preferred[0]['user_id']) if preferred else ''
        by_page = defaultdict(list)
        for row in rows:
            page = str(row['page_id'])
            if row['value'] not in {item['value'] for item in by_page[page]}:
                by_page[page].append(row)
        for page, values in by_page.items():
            values.sort(key=lambda row:str(row['user_id'])!=preferred_user)
        return by_page

    def revenue(self, tasks, now):
        # Three Beijing reporting dates, plus three days for attribution repair.
        # This is deliberately not labelled an exact rolling 72-hour measure.
        mature = [t for t in tasks if (now.astimezone(BJ).date()-datetime.fromisoformat(t['created_at_utc']).astimezone(BJ).date()).days>=6]
        if not mature:
            return {}
        page_ids = {str(t['page_id']) for t in mature}
        if len(page_ids)!=1:
            raise FeedbackError('feedback_revenue_page_scope_invalid')
        dates = [datetime.fromisoformat(t['created_at_utc']).astimezone(BJ).date() for t in mature]
        start, end = min(dates).isoformat(), (max(dates)+timedelta(days=2)).isoformat()
        sql = "SELECT /*+ MAX_EXECUTION_TIME(15000) */ JSON_OBJECT('dt',dt,'campaign_id',campaign_id,'campaign',campaign,'material_id',ad_id,'amount',SUM(dnu_revenue)) FROM ads_facebook_page_insight FORCE INDEX(adset_dt) WHERE adset_id IN ("+ids_sql(page_ids)+") AND dt BETWEEN '"+start+"' AND '"+end+"' AND site_id='2049' AND ad_id IN ("+ids_sql({t['material_id'] for t in mature})+") GROUP BY dt,campaign_id,campaign,ad_id LIMIT 20001"
        rows = self.query(sql)
        if len(rows)>20000:
            raise FeedbackError('feedback_revenue_scope_too_large')
        by_id = {str(t['id']):t for t in mature}
        totals = {int(t['id']):0.0 for t in mature}
        for row in rows:
            # Validate both candidates against the exact Page/material lineage.
            candidates = [str(row.get('campaign_id') or ''),str(row.get('campaign') or '').rsplit('*',1)[-1]]
            task = next((by_id[key] for key in candidates if key in by_id and str(by_id[key]['material_id'])==str(row['material_id'])),None)
            if task is None:
                continue
            day = datetime.fromisoformat(task['created_at_utc']).astimezone(BJ).date()
            if day.isoformat() <= str(row['dt']) <= (day+timedelta(days=2)).isoformat():
                totals[int(task['id'])] += float(row['amount'] or 0)
        return totals


class MetaReader:
    def __init__(self, version, *, max_calls=400, interval=0.5):
        if not re.fullmatch(r'v[0-9]+\.[0-9]+',version):
            raise FeedbackError('feedback_graph_version_invalid')
        self.version, self.max_calls, self.interval = version, max_calls, interval
        self.calls, self.last = 0, 0.0
        self.session = requests.Session()
        self.session.trust_env = False

    def get(self, params, value):
        if self.calls >= self.max_calls:
            raise FeedbackError('feedback_request_budget_reached')
        time.sleep(max(0,self.interval-(time.monotonic()-self.last)))
        self.calls += 1
        self.last = time.monotonic()
        try:
            response = self.session.get('https://graph.facebook.com/'+self.version+'/',params=params,
                headers={'Authorization':'Bearer '+value},timeout=(8,40),allow_redirects=False)
            payload = response.json()
        except (requests.RequestException, ValueError):
            raise FeedbackError('feedback_graph_transport_failed') from None
        for header in ('x-app-usage','x-page-usage','x-business-use-case-usage'):
            def usage_high(value):
                if isinstance(value,dict):
                    return any((key in ('call_count','total_cputime','total_time') and isinstance(v,(int,float)) and v>=85) or usage_high(v) for key,v in value.items())
                if isinstance(value,list):
                    return any(usage_high(v) for v in value)
                return False
            try:
                if usage_high(json.loads(response.headers.get(header,'{}'))):
                    raise FeedbackError('feedback_graph_usage_high')
            except ValueError:
                pass
        if not isinstance(payload,dict) or response.status_code!=200 or 'error' in payload:
            code = payload.get('error',{}).get('code','unknown') if isinstance(payload,dict) else 'unknown'
            raise FeedbackError('feedback_graph_'+str(code)[:12])
        return payload


def parse_metrics(raw):
    values = {item['name']:item['values'][0]['value'] for item in raw.get('insights',{}).get('data',[]) if item.get('values')}
    if not isinstance(values.get('post_media_view'),int) or not isinstance(values.get('post_clicks_by_type'),dict):
        raise FeedbackError('feedback_insights_incomplete')
    clicks = values['post_clicks_by_type'].get('link clicks',0)
    if not isinstance(clicks,int) or clicks<0:
        raise FeedbackError('feedback_clicks_invalid')
    watch = values.get('post_video_avg_time_watched')
    return {'media_views':values['post_media_view'],'link_clicks':clicks,
            'avg_watch_seconds':watch/1000 if isinstance(watch,(int,float)) else None}


def collect(conn, reader, graph, *, now=None, max_posts=750):
    now = now or datetime.now(UTC)
    started = time.monotonic()
    groups = set()
    for row in conn.execute("SELECT v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.status='enabled'"):
        config = json.loads(row[0])
        if config.get('feedback_selection',{}).get('enabled'):
            groups.update(config['group_ids'])
    if not groups:
        return {'status':'no_feedback_templates','collected':0,'failed':0,'requests':0}
    rows = conn.execute("""SELECT t.id,t.page_id,t.graph_post_id,t.material_id,t.completed_at_utc,p.language,
        e.post_id,e.created_at_utc,e.collected_at_utc,a.retry_at_utc FROM fb_auto_task t
        JOIN fb_auto_run_page p ON p.run_id=t.run_id AND p.page_id=t.page_id
        JOIN fb_auto_publish_ledger l ON l.task_id=t.id AND l.status='published' AND l.graph_post_id=t.graph_post_id
        LEFT JOIN fb_auto_post_effect e ON e.task_id=t.id LEFT JOIN fb_auto_post_effect_attempt a ON a.task_id=t.id
        WHERE t.status='published' AND t.group_id IN ("""+','.join('?' for _ in groups)+""")
        AND t.completed_at_utc>=? AND t.completed_at_utc<=? ORDER BY COALESCE(e.collected_at_utc,''),t.id""",
        (*sorted(groups),(now-timedelta(days=21)).isoformat(timespec='seconds'),(now-timedelta(hours=24)).isoformat(timespec='seconds'))).fetchall()
    stages = defaultdict(set)
    for row in conn.execute('SELECT task_id,stage FROM fb_auto_post_effect_snapshot'):
        stages[row[0]].add(row[1])
    wanted = []
    for raw in rows:
        task = dict(raw)
        if task['retry_at_utc'] and task['retry_at_utc']>now.isoformat(timespec='seconds'):
            continue
        origin = task['created_at_utc'] or task['completed_at_utc']
        age = (now-datetime.fromisoformat(origin)).total_seconds()/3600
        stage = next((name for name,h in [('24h',24),('72h',72),('7d',168)] if h<=age<=h+6 and name not in stages[task['id']]),'latest')
        if stage=='latest' and task['collected_at_utc'] and task['collected_at_utc']>(now-timedelta(hours=24)).isoformat(timespec='seconds'):
            continue
        task['stage']=stage
        wanted.append(task)
    wanted.sort(key=lambda t:(t['stage']=='latest',t['collected_at_utc'] or '',t['id']))
    wanted = wanted[:max_posts]
    if not wanted:
        return {'status':'up_to_date','collected':0,'failed':0,'requests':0}
    credentials = reader.credentials(sorted({t['page_id'] for t in wanted}))
    by_page = defaultdict(list)
    for task in wanted:
        by_page[task['page_id']].append(task)
    collected, failed, errors = 0, 0, defaultdict(int)
    for page_id,tasks in by_page.items():
        for offset in range(0,len(tasks),25):
            batch = tasks[offset:offset+25]
            remaining = {t['id']:t for t in batch}
            error = 'feedback_no_readable_authorization'
            for credential in credentials.get(page_id,[])[:3]:
                if not remaining:
                    break
                try:
                    missing = [t for t in remaining.values() if not t['post_id']]
                    if missing:
                        data = graph.get({'ids':','.join(t['graph_post_id'] for t in missing),'fields':'id,post_id,created_time'},credential['value'])
                        for task in missing:
                            item = data.get(task['graph_post_id'],{})
                            native = str(item.get('post_id') or '')
                            if native and str(item.get('id')) == task['graph_post_id']:
                                task['post_id'] = native if '_' in native else page_id+'_'+native
                                task['created_at_utc'] = graph_created_at(item.get('created_time'))
                    ready = [t for t in remaining.values() if t['post_id']]
                    if not ready:
                        raise FeedbackError('feedback_video_post_mapping_unavailable')
                    data = graph.get({'ids':','.join(t['post_id'] for t in ready),'fields':'id,insights.metric('+METRICS+').period(lifetime)'},credential['value'])
                    try:
                        revenue = reader.revenue(ready,now)
                    except FeedbackError:
                        revenue = {}  # Unknown income never becomes zero.
                    for task in ready:
                        try:
                            item = data.get(task['post_id'],{})
                            if str(item.get('id')) != task['post_id']:
                                raise FeedbackError('feedback_post_unavailable')
                            values = parse_metrics(item)
                            captured = now+timedelta(seconds=time.monotonic()-started)
                            age = (captured-datetime.fromisoformat(task['created_at_utc'])).total_seconds()/3600
                            stage = next((name for name,h in [('24h',24),('72h',72),('7d',168)] if h<=age<=h+6 and name not in stages[task['id']]),'latest')
                            with conn:
                                write_effect(conn,task['id'],{'page_id':page_id,'video_id':task['graph_post_id'],
                                    'post_id':task['post_id'],'language':task['language'],
                                    'created_at_utc':task['created_at_utc'],'collected_at_utc':captured.isoformat(timespec='seconds'),
                                    'dnu_revenue_d3':revenue.get(task['id']),
                                    'revenue_window':'beijing_publish_day_plus_2' if task['id'] in revenue else '',**values},stage=stage)
                                conn.execute("DELETE FROM fb_auto_post_effect_attempt WHERE task_id=?",(task['id'],))
                            collected += 1
                            remaining.pop(task['id'])
                        except (FeedbackError,ValueError):
                            error = 'feedback_post_metrics_unavailable'
                except FeedbackError as exc:
                    error = str(exc)
                    if error in ('feedback_request_budget_reached','feedback_graph_usage_high'):
                        return {'status':error,'collected':collected,'failed':failed,'requests':graph.calls,'errors':dict(errors)}
            for task in remaining.values():
                failed += 1
                errors[error] += 1
                with conn:
                    conn.execute('INSERT OR REPLACE INTO fb_auto_post_effect_attempt VALUES(?,?,?,?)',
                        (task['id'],now.isoformat(timespec='seconds'),(now+timedelta(hours=6)).isoformat(timespec='seconds'),error))
    return {'status':'complete','collected':collected,'failed':failed,'requests':graph.calls,'errors':dict(errors)}
