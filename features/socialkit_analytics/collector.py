"""Read-only sources and attribution. No raw task-to-revenue joins."""
import contextlib
import json
import os
import subprocess
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone

from .schema import (CREATION_SOURCES, EFFECT_METRICS, MATERIAL_TYPES, PLATFORM_ACCOUNT,
                     PLATFORM_LINK, PRODUCTS, empty_common, stable_key)


class CollectionError(RuntimeError):
    pass


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
        sql = ("SET SESSION time_zone='+00:00'; SET SESSION MAX_EXECUTION_TIME=25000; "
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
                if process.stdout.readline().strip() != '1':
                    raise CollectionError('internal read endpoint did not prove read_only=1')
                for line in process.stdout:
                    if line.strip():
                        try:
                            value = json.loads(bytes.fromhex(line.strip()).decode('utf-8'))
                        except (ValueError, UnicodeError):
                            raise CollectionError('internal source returned an invalid result') from None
                        yield value
                if process.wait(timeout=10) != 0 or timed_out.is_set():
                    raise CollectionError('internal source query failed or timed out')
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
        return list(self.records(statements))


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
    return facts, control


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
    """Yield one complete day at a time; a trailing CLI failure still aborts publication."""
    days = days_between(start, end)
    accounts, dramas, teams = source.dimensions()
    dimensions = Dimensions(accounts, dramas)
    statements = [statement for day in days for statement in income_statements(day)]
    current = []
    seen = []
    for record in reader.records(statements):
        if record.get('kind') == 'effect':
            current.append(record)
        elif record.get('kind') == 'control':
            day = record['date']
            if day not in days or day in seen or any(row['date'] != day for row in current):
                raise CollectionError('source result dates are inconsistent')
            expected = {field: int(record[field]) for field in EFFECT_METRICS}
            if control_totals(current) != expected:
                raise CollectionError('income source control totals do not match grouped facts')
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
            updated = str(record['source_updated_at'])
            if updated:
                updated = datetime.fromisoformat(updated).replace(tzinfo=timezone.utc).isoformat(timespec='seconds')
            yield dict(date=day, effects=effects, publications=publications, totals=expected,
                       source_rows=int(record['source_rows']), source_updated_at=updated,
                       quality=dict(unmatched_rows=len(unknown), unmatched=control_totals(unknown)))
            current = []
            seen.append(day)
        else:
            raise CollectionError('unexpected source result kind')
    if seen != days or current:
        raise CollectionError('not every requested source date completed')
