"""Read-only sources and attribution. No raw task-to-revenue joins."""
import contextlib
from collections import defaultdict
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from features.social_analytics.bill_income import FIRST_DAY, merge_settled, window

from .schema import (CREATION_SOURCES, EFFECT_METRICS, MATERIAL_TYPES, PLATFORM_ACCOUNT,
                     PLATFORM_LINK, PRODUCTS, empty_common, stable_key)


class CollectionError(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def reader_failure(message, stderr):
    # Classify only numeric connection errors. Never expose connector stderr or retry auth/query errors.
    codes = {int(value) for value in re.findall(rb'ERROR\s+(\d+)', stderr)}
    return CollectionError(message, retryable=bool(codes) and codes.issubset({1053, 1158, 1159, 1160, 1161, 2002, 2003, 2006, 2013}))


def days_between(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError('start must be on or before end')
    return [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]


def mysql_settings(env, role):
    primary = 'SOCIALKIT_ANALYTICS_%s_MYSQL_' % ('WRITE' if role == 'writer' else role.upper())
    fallback = {'source': 'SOCIALKIT_TT_SOURCE_MYSQL_', 'reader': 'ADMIN_MAPPING_MYSQL_',
                'writer': 'SOCIALKIT_TT_TARGET_MYSQL_'}[role]
    def value(key, default=''):
        return env.get(primary + key, env.get(fallback + key, default))
    config = dict(host=value('HOST'), port=int(value('PORT', '0')), user=value('USER'),
                  password=value('PASSWORD'), database=value('DATABASE'))
    expected = {'source': ('hk-cynosdbmysql-grp-dejftnkp.sql.tencentcdb.com', 28914),
                'reader': ('101.32.56.53', 63350), 'writer': ('101.32.56.53', 63353)}[role]
    if (config['host'], config['port']) != expected or not config['user'] or not config['password']:
        raise CollectionError('%s database settings are missing or target an unexpected endpoint' % role)
    if role == 'writer':
        config['database'] = 'ads_ai'
    return config


class GatedReader:
    """One gated mysql process/connection for a sequence of read statements."""
    def __init__(self, env, timeout=600):
        self.config = mysql_settings(env, 'reader')
        self.environment = dict(env)
        self.timeout = timeout

    def records(self, statements):
        env = dict(os.environ)
        env.update(self.environment)
        env.pop('SQL_GATE_BYPASS', None)
        env.update(MYSQL_PWD=self.config['password'], SQL_GATE_JOB_NAME='socialkit-analytics',
                   SQL_GATE_WAIT_TIMEOUT_SECONDS='60', SQL_GATE_EXEC_TIMEOUT_SECONDS=str(self.timeout),
                   SQL_GATE_RETRY_DELAYS_SECONDS='')
        sql = ("SET SESSION time_zone='+00:00'; SET SESSION MAX_EXECUTION_TIME=120000; "
               "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ; "
               "START TRANSACTION READ ONLY; SELECT @@read_only;\n" +
               ';\n'.join(statements) + ';\nROLLBACK;\n')
        args = ['/usr/bin/mysql', '-h', self.config['host'], '-P', str(self.config['port']),
                '-u', self.config['user'], '--connect-timeout=5', '--default-character-set=utf8mb4',
                '--skip-reconnect', '-N', '-B', '-r']
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=errors, env=env, text=True, encoding='utf-8')
            timed_out = threading.Event()
            def timeout():
                timed_out.set()
                process.terminate()
            timer = threading.Timer(self.timeout + 70, timeout)
            timer.daemon = True
            timer.start()
            try:
                def write_input():
                    with contextlib.suppress(BrokenPipeError, OSError):
                        process.stdin.write(sql)
                        process.stdin.close()
                input_thread = threading.Thread(target=write_input, daemon=True)
                input_thread.start()
                proof = process.stdout.readline().strip()
                if proof != '1':
                    if not proof:
                        process.wait(timeout=10)
                        errors.seek(0)
                        raise reader_failure('internal read endpoint did not prove read_only=1', errors.read())
                    raise CollectionError('internal read endpoint did not prove read_only=1')
                for line in process.stdout:
                    if line.strip():
                        try:
                            value = json.loads(bytes.fromhex(line.strip()).decode('utf-8'))
                        except (ValueError, UnicodeError):
                            raise CollectionError('internal source returned an invalid result') from None
                        yield value
                if process.wait(timeout=10) != 0 or timed_out.is_set():
                    errors.seek(0)
                    raise reader_failure('internal source query failed or timed out', errors.read())
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()

    def query(self, statements):
        for attempt in range(3):
            try:
                return list(self.records(statements))
            except CollectionError as exc:
                if not exc.retryable or attempt == 2:
                    raise
                # Gate forwards stdout directly; discard the entire failed attempt before retrying.
                print(json.dumps(dict(event='source_read_retry', attempt=attempt + 2)), flush=True)
                time.sleep((2, 5)[attempt])


def _income_metrics_sql():
    fields = dict(clicks='clicks', views='views', installs='installs', conversions='recharge',
                  dnu_conversions='dnu_recharge')
    values = ["'%s',COALESCE(SUM(%s),0)" % (key, column) for key, column in fields.items()]
    values += ["'%s',CAST(ROUND(COALESCE(SUM(%s),0)*100) AS SIGNED)" % (key, column)
               for key, column in [('revenue_cents', 'revenue'), ('refund_cents', 'refund_revenue'),
                                   ('dnu_revenue_cents', 'dnu_revenue')]]
    return ','.join(values)


def income_statements(day):
    day = date.fromisoformat(day).isoformat()
    where = " FROM kunlunads_dev.ads_facebook_page_insight FORCE INDEX(c_dt) WHERE channel='socialkit' AND dt='%s'" % day
    metrics = _income_metrics_sql()
    facts = ("SELECT HEX(JSON_OBJECT('kind','effect','date','%s','site_id',site_id,"
             "'short_code',MIN(campaign),'country',MIN(country),'device_os',MIN(platform),%s))%s "
             "GROUP BY dt,site_id,BINARY campaign,BINARY country,BINARY platform" % (day, metrics, where))
    control = ("SELECT HEX(JSON_OBJECT('kind','control','date','%s','source_rows',COUNT(*),"
               "'source_updated_at',COALESCE(CAST(MAX(updated_at) AS CHAR),''),%s))%s" % (day, metrics, where))
    if day < FIRST_DAY:
        return facts, control
    start, stop = window(day)
    bills_where = f""" FROM kunlunads_dev.ads_drama_bills FORCE INDEX(idx_event_cursor_filter)
        WHERE event_time >= {start} AND event_time < {stop}
          AND af_channel='socialkit' AND event_name IN ('af_purchase','ad_revenue')"""
    amounts = """'iap_cents',CAST(COALESCE(SUM(IF(event_name='af_purchase',event_revenue_usd,0)),0)*100 AS SIGNED),
                 'iaa_cents',CAST(COALESCE(SUM(IF(event_name='ad_revenue',event_revenue_usd,0)),0)*100 AS SIGNED),
                 'ad_events',COALESCE(SUM(event_name='ad_revenue'),0),
                 'purchase_events',COALESCE(SUM(event_name='af_purchase'),0),'updated_at',CAST(MAX(updated_at) AS CHAR)"""
    grouped = (f"SELECT HEX(JSON_OBJECT('kind','bill','date','{day}','site_id',site_id,"
               f"'short_code',MIN(campaign),'country',MIN(country_code),'device_os',MIN(platform),{amounts}))"
               + bills_where + " GROUP BY site_id,BINARY campaign,BINARY country_code,BINARY platform")
    bill_control = f"SELECT HEX(JSON_OBJECT('kind','bill_control','date','{day}',{amounts}))" + bills_where
    return facts, control, grouped, bill_control


def provisional_purchases(day, site):
    start, stop = window(day)
    site_literal = "X'" + str(site or "").encode("utf-8").hex() + "'"
    return f"""SELECT HEX(JSON_OBJECT('date','{day}','site_id',site_id,'short_code',MIN(campaign),
        'country',MIN(country_code),'device_os',MIN(platform),
        'iap_cents',CAST(SUM(event_revenue_usd)*100 AS SIGNED),'iaa_cents',0,'ad_events',0,
        'updated_at',CAST(MAX(updated_at) AS CHAR)))
        FROM kunlunads_dev.ads_drama_bills_t0 FORCE INDEX(idx_site_event_name_time)
        WHERE site_id={site_literal} AND event_name='af_purchase' AND af_channel='socialkit'
          AND event_time >= {start} AND event_time < {stop}
        GROUP BY site_id,BINARY campaign,BINARY country_code,BINARY platform"""


class SocialKitSource:
    def __init__(self, env):
        self.config = mysql_settings(env, 'source')
        self.connection = None

    def __enter__(self):
        import pymysql
        self.connection = pymysql.connect(**self.config, charset='utf8mb4', connect_timeout=5,
                                          read_timeout=35, write_timeout=5, autocommit=False,
                                          cursorclass=pymysql.cursors.DictCursor)
        with self.connection.cursor() as cursor:
            cursor.execute('SET SESSION MAX_EXECUTION_TIME=25000')
            cursor.execute("SET SESSION time_zone='+00:00'")
            cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            cursor.execute('START TRANSACTION READ ONLY')
        return self

    def __exit__(self, *args):
        if self.connection:
            with contextlib.suppress(Exception):
                self.connection.rollback()
            self.connection.close()

    def query(self, sql, values=()):
        with self.connection.cursor() as cursor:
            cursor.execute(sql, values)
            return cursor.fetchall()

    def dimensions(self):
        accounts = self.query('SELECT id,team_id,external_account_id,account_name,platform,is_deleted '
                              'FROM socialkit.social_center_accounts')
        teams = self.query('SELECT DISTINCT team_id FROM socialkit.social_auto_publish_task FORCE INDEX(idx_team_date_del)')
        return accounts, [], sorted({int(t['team_id']) for t in teams})

    def drama_names(self, keys):
        # Display names only: read just referenced content IDs, then retain exact app/content keys.
        keys = set(keys)
        content_ids = sorted({content for _, content in keys if content})
        result = []
        for offset in range(0, len(content_ids), 500):
            batch = content_ids[offset:offset + 500]
            rows = self.query('SELECT drama_app_id,drama_content_id,MAX(drama_name) drama_name '
                              'FROM socialkit.social_auto_publish_task FORCE INDEX(idx_drama_account_del) '
                              'WHERE drama_content_id IN (' + ','.join(['%s'] * len(batch)) + ') '
                              'GROUP BY drama_app_id,drama_content_id', batch)
            result += [row for row in rows if (int(row['drama_app_id']), str(row['drama_content_id'])) in keys]
        return result

    def links(self, codes):
        result = []
        codes = sorted(set(code for code in codes if code))
        for start in range(0, len(codes), 1000):
            batch = codes[start:start + 1000]
            result += self.query('SELECT id,short_code,team_id,drama_app_id,drama_content_id,external_account_id,'
                                 'platform_code,material_id,material_pool_type,creation_source,created_at,deleted_at '
                                 'FROM socialkit.social_short_link WHERE short_code IN (' +
                                 ','.join(['%s'] * len(batch)) + ')', batch)
        # The source index is case-insensitive; matching is nevertheless exact in Python.
        return {row['short_code']: row for row in result}

    def publications(self, day, teams):
        result = []
        control = dict(tasks=0, planned=0, success=0, failed=0)
        for team in teams:
            result += self.query('SELECT id,team_id,schedule_date,account_platform,account_id,drama_app_id,'
                                 'drama_content_id,drama_name,product,account_lang,account_country,reason,'
                                 'task_status,item_total,item_success,item_failed,updated_at '
                                 'FROM socialkit.social_auto_publish_task FORCE INDEX(idx_team_date_del) '
                                 'WHERE team_id=%s AND schedule_date=%s AND deleted_at=0', (team, day))
            totals = self.query('SELECT COUNT(*) tasks,COALESCE(SUM(item_total),0) planned,'
                                'COALESCE(SUM(item_success),0) success,COALESCE(SUM(item_failed),0) failed '
                                'FROM socialkit.social_auto_publish_task FORCE INDEX(idx_team_date_del) '
                                'WHERE team_id=%s AND schedule_date=%s AND deleted_at=0', (team, day))[0]
            for field in control:
                control[field] += int(totals[field])
        actual = dict(tasks=len(result), planned=sum(int(row['item_total']) for row in result),
                      success=sum(int(row['item_success']) for row in result),
                      failed=sum(int(row['item_failed']) for row in result))
        if actual != control:
            raise CollectionError('publication source control totals do not match task facts')
        return result


class Dimensions:
    def __init__(self, accounts, dramas):
        self.accounts_by_id = {int(a['id']): a for a in accounts}
        self.accounts = {}
        for account in accounts:
            key = (int(account['team_id']), PLATFORM_ACCOUNT.get(int(account['platform']), 'unattributed'),
                   str(account['external_account_id']))
            self.accounts.setdefault(key, []).append(account)
        self.dramas = {(int(d['drama_app_id']), str(d['drama_content_id'])): str(d['drama_name']) for d in dramas}

    def common(self, day, team, platform, external_id, app_id, content_id, name=''):
        value = empty_common(day)
        team, app_id = int(team), int(app_id)
        external_id, content_id = str(external_id or ''), str(content_id or '')
        value.update(team_id=team, platform=platform, product=PRODUCTS.get(app_id, 'App %s' % app_id),
                     external_account_id=external_id, drama_app_id=app_id, drama_content_id=content_id,
                     account=stable_key(team, platform, external_id) if external_id else '',
                     drama=stable_key(app_id, content_id) if content_id else '',
                     drama_name=name or self.dramas.get((app_id, content_id), content_id))
        candidates = self.accounts.get((team, platform, external_id), [])
        active = [a for a in candidates if not a['is_deleted']]
        chosen = active if len(active) == 1 else candidates
        value['account_name'] = str(chosen[0]['account_name']) if len(chosen) == 1 else external_id
        return value

    def effect(self, raw, links):
        link = links.get(raw['short_code'])
        value = empty_common(raw['date'])
        value.update({field: str(raw.get(field) or '') for field in ('site_id', 'short_code', 'country', 'device_os')})
        value.update({field: int(raw.get(field) or 0) for field in EFFECT_METRICS})
        value['iaa_cents'] = raw.get('iaa_cents')
        value.update(material_type='unknown', creation_source='unknown', matched=0)
        if link:
            value.update(self.common(raw['date'], link['team_id'],
                                     PLATFORM_LINK.get(int(link['platform_code']), 'unattributed'),
                                     link['external_account_id'], link['drama_app_id'], link['drama_content_id']))
            value.update(matched=1, creation_source=CREATION_SOURCES.get(int(link['creation_source']), 'unknown'),
                         material_type=MATERIAL_TYPES.get(int(link['material_pool_type']), 'unknown')
                         if int(link['material_id']) else 'none')
        return value

    def publication(self, raw):
        account = self.accounts_by_id.get(int(raw['account_id']), {})
        external = account.get('external_account_id') or 'socialkit-account:%s' % raw['account_id']
        value = self.common(raw['schedule_date'], raw['team_id'],
                            PLATFORM_ACCOUNT.get(int(raw['account_platform']), 'unattributed'), external,
                            raw['drama_app_id'], raw['drama_content_id'], str(raw['drama_name'] or ''))
        value.update(task_id=int(raw['id']), language=str(raw['account_lang'] or ''),
                     account_country=str(raw['account_country'] or ''), reason=str(raw['reason'] or ''),
                     task_status=int(raw['task_status']), planned=int(raw['item_total']),
                     success=int(raw['item_success']), failed=int(raw['item_failed']), tasks=1)
        return value


def control_totals(rows):
    return {field: sum(int(row[field]) for row in rows) for field in EFFECT_METRICS}


def collect_days(reader, source, start, end):
    """Release each day's gated RR read before external attribution or warehouse writes."""
    days = days_between(start, end)
    accounts, dramas, teams = source.dimensions()
    dimensions = Dimensions(accounts, dramas)
    for day in days:
        # Exhaustion also checks the CLI exit status and releases the FIFO permit.
        # Both fact and control statements still share one readonly RR transaction.
        records = reader.query(income_statements(day))
        current = []
        control = None
        bills, bill_control = [], None
        for record in records:
            if record.get('date') != day:
                raise CollectionError('source result dates are inconsistent')
            if record.get('kind') == 'effect' and control is None:
                current.append(record)
            elif record.get('kind') == 'control' and control is None:
                control = record
            elif record.get('kind') == 'bill':
                bills.append(record)
            elif record.get('kind') == 'bill_control' and bill_control is None:
                bill_control = record
            else:
                raise CollectionError('unexpected source result kind or order')
        if control is None:
            raise CollectionError('not every requested source date completed')
        expected = {field: int(control[field]) for field in EFFECT_METRICS}
        if control_totals(current) != expected:
            raise CollectionError('income source control totals do not match grouped facts')
        settled = False
        covered_sites = set()
        pending_sites = set()
        if day >= FIRST_DAY:
            if bill_control is None or any(sum(row[k] for row in bills) != bill_control[k]
                                           for k in ('iap_cents', 'iaa_cents', 'ad_events', 'purchase_events')):
                raise CollectionError('social bill totals do not match grouped facts')
            site_bills = defaultdict(list)
            for bill in bills:
                site_bills[bill["site_id"]].append(bill)
            covered_sites = {site for site, group in site_bills.items() if any(b["ad_events"] for b in group)}
            pending_sites = {r["site_id"] for r in current + bills} - covered_sites
            provisional_cents = 0
            for site in (covered_sites | pending_sites) if covered_sites else ():
                if not any(b["purchase_events"] for b in site_bills[site]):
                    preliminary = reader.query([provisional_purchases(day, site)])
                    provisional_cents += sum(b["iap_cents"] for b in preliminary)
                    bills.extend(preliminary)
            current, settled = merge_settled(current, bills, ('site_id', 'short_code', 'country', 'device_os'),
                                              dict.fromkeys(EFFECT_METRICS, 0))
            if settled:
                expected['revenue_cents'] = bill_control['iap_cents'] + bill_control['iaa_cents'] + provisional_cents
                if control_totals(current) != expected or sum(row['iaa_cents'] for row in current) != bill_control['iaa_cents']:
                    raise CollectionError('merged income failed reconciliation')
                for row in current:
                    if row["site_id"] not in covered_sites:
                        row["iaa_cents"] = None
        links = source.links(row['short_code'] for row in current)
        required_names = {(int(link['drama_app_id']), str(link['drama_content_id'])) for link in links.values()}
        missing_names = required_names - set(dimensions.dramas)
        if missing_names:
            names = source.drama_names(missing_names)
            dimensions.dramas.update({(int(row['drama_app_id']), str(row['drama_content_id'])):
                                     str(row['drama_name'] or row['drama_content_id']) for row in names})
            # An unavailable display name must not change attribution or trigger repeated reads.
            for key in missing_names:
                dimensions.dramas.setdefault(key, key[1])
        effects = [dimensions.effect(row, links) for row in current]
        publications = [dimensions.publication(row) for row in source.publications(day, teams)]
        unknown = [row for row in effects if not row['matched']]
        updated = max(str(control['source_updated_at'] or ''), str((bill_control or {}).get('updated_at') or ''))
        if updated:
            updated = datetime.fromisoformat(updated).replace(tzinfo=timezone.utc).isoformat(timespec='seconds')
        yield dict(date=day, effects=effects, publications=publications, totals=expected,
                   source_rows=int(control['source_rows']), source_updated_at=updated,
                   quality=dict(unmatched_rows=len(unknown), unmatched=control_totals(unknown),
                                iaa_covered=settled, iaa_cents=bill_control['iaa_cents'] if settled else None,
                                iaa_sites=sorted(covered_sites), iaa_pending_sites=sorted(pending_sites)))
