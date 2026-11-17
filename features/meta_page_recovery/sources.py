"""Bounded read-only SQL discovery and product-specific drama identity."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json
import os
import subprocess
import re

from .domain import POOLS, language, UNKNOWN


def literal(value):
    return '0x' + str(value).encode().hex() if str(value) else "''"


def environment(path='/etc/meta-page-recovery.env'):
    env = {}
    for name in ['/root/drama_material_service/.env', '/etc/fb-page-assignment.env', path]:
        p = Path(name)
        if p.is_file():
            for line in p.read_text().splitlines():
                if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    env[key.strip()] = value.strip().strip('"').strip("'")
    env.update(os.environ)
    return env


def shared(env):
    import features
    path = str(Path(env.get('META_PAGE_RECOVERY_SHARED_ROOT', '/root/drama_material_service')) / 'features')
    if path not in features.__path__:
        features.__path__.append(path)


class SourceError(Exception):
    pass


class Reader:
    def __init__(self, env):
        self.env = env
        if (env.get('ADMIN_MAPPING_MYSQL_HOST'), str(env.get('ADMIN_MAPPING_MYSQL_PORT'))) != ('101.32.56.53', '63350'):
            raise SourceError('invalid_reader_endpoint')

    def query(self, statements, timeout=45):
        env = dict(os.environ)
        env.pop('SQL_GATE_BYPASS', None)
        env.update(MYSQL_PWD=self.env['ADMIN_MAPPING_MYSQL_PASSWORD'], SQL_GATE_JOB_NAME='meta-page-recovery',
                   SQL_GATE_WAIT_TIMEOUT_SECONDS='30', SQL_GATE_EXEC_TIMEOUT_SECONDS=str(timeout), SQL_GATE_RETRY_DELAYS_SECONDS='')
        sql = ("delimiter //\nSET SESSION time_zone='+00:00',MAX_EXECUTION_TIME=8000,tx_isolation='REPEATABLE-READ'; "
               "START TRANSACTION READ ONLY; SELECT @@read_only;\n"+';\n'.join(statements)+';\nROLLBACK//\n')
        try:
            result = subprocess.run(['/usr/bin/mysql','-h','101.32.56.53','-P','63350','-u',self.env['ADMIN_MAPPING_MYSQL_USER'],
                                     '--connect-timeout=3','--skip-reconnect','--default-character-set=utf8mb4','--compress','-N','-B','-r'],
                                    input=sql,env=env,text=True,encoding='utf-8',capture_output=True,timeout=timeout+40)
            lines = result.stdout.splitlines()
            if result.returncode or not lines or lines.pop(0) != '1':
                codes = re.findall(r'ERROR (\d+)', result.stderr)
                raise SourceError('source_query_failed_'+'_'.join(codes))
            return [json.loads(bytes.fromhex(line).decode()) for line in lines if line.strip()]
        except subprocess.TimeoutExpired:
            raise SourceError('source_query_timeout') from None
        except (ValueError, OSError):
            raise SourceError('source_response_invalid') from None


class Sources:
    def __init__(self, env, reader=None):
        self.env, self.reader = env, reader or Reader(env)
        self.token_cache = {}

    def pages(self):
        # The group and membership conditions are the authorization scope, not
        # dated account/product allowlists from unrelated old launchers.
        result = self.reader.query(["""SELECT HEX(JSON_OBJECT('group_id',g.id,'page_id',i.page_id,'language',i.language,
            'status',p.status,'forbidden_status',p.forbidden_status,'is_permission',p.is_permission,
            'status_at',p.status_at,'active_ad_count',p.active_ad_count,'fan_ccount',p.fan_ccount,
            'updated_at',CAST(p.updated_at AS CHAR)))
            FROM kunlunads_dev.ads_facebook_page_group g
            JOIN kunlunads_dev.ads_facebook_page_group_ins i ON i.group_id=g.id AND i.deleted_at=0
            LEFT JOIN kunlunads_dev.ads_facebook_page_ins p ON p.page_id=i.page_id
            WHERE g.id IN (24,25,57) AND g.is_delete=0 AND TRIM(i.page_id)<>'' ORDER BY g.id,i.page_id LIMIT 20001"""])
        if len(result)>20000:
            raise SourceError('page_scope_overflow')
        return result

    def ads(self, page_id, cursor='', size=500):
        rows = self.reader.query(["""SELECT HEX(JSON_OBJECT('ad_id',c.ad_id,'ad_account_id',c.ad_account_id,
            'creative_id',c.creative_id,'user_id',c.user_id,'product',c.product,'app_id',c.app_id,
            'language',c.language,'source_id',c.source_id,'original_source_id',c.original_source_id,
            'is_dynamic',c.is_dynamic))
            FROM kunlunads_dev.ads_facebook_auto_page p FORCE INDEX(idx_ps)
            JOIN kunlunads_dev.ads_facebook_auto_created_data c ON c.ad_id=p.ad_id
            WHERE p.page_id=%s AND p.status='ACTIVE' AND c.status='ACTIVE' AND p.ad_id>%s
            ORDER BY p.ad_id LIMIT %d""" % (literal(page_id), literal(cursor), size)])
        unique = {}
        for row in rows:
            aid = str(row['ad_id'])
            if aid in unique and unique[aid]['ad_account_id'] != row['ad_account_id']:
                raise SourceError('ad_account_identity_conflict')
            unique[aid] = row
        return list(unique.values()), max((str(r['ad_id']) for r in rows), default=cursor), len(rows)<size

    def tokens(self, job):
        key = (str(job.get('user_id')), str(job['ad_account_id']))
        if key in self.token_cache:
            return self.token_cache[key]
        ids = [str(job.get('user_id') or '0')]
        ids.extend(x.strip() for x in self.env.get('META_PAGE_RECOVERY_TOKEN_USER_IDS','803,804').split(',') if x.strip().isdigit())
        if any(not x.isdigit() for x in ids):
            raise SourceError('invalid_token_user_identity')
        values = self.reader.query(["SELECT HEX(JSON_OBJECT('user_id',user_id,'token',accessToken)) "
             "FROM kunlunads_dev.ads_facebook_info WHERE user_id IN ("+','.join(ids)+") AND accessToken<>'' LIMIT 100"])
        preferred = sorted(values, key=lambda r: str(r['user_id'])!=ids[0])
        tokens = list(dict.fromkeys(str(r['token']) for r in preferred if r.get('token')))
        if self.env.get('META_PAGE_RECOVERY_ACCESS_TOKEN'):
            tokens.insert(0,self.env['META_PAGE_RECOVERY_ACCESS_TOKEN'])
        self.token_cache[key] = tokens
        return tokens

    def target(self, job, old_page):
        """The source adapter supplies exact catalog identities, never ID-only joins."""
        app_id = POOLS[job['group_id']][0]
        source = str(job.get('source_id') or '')
        result = dict(language=language(old_page.get('language')), audience=UNKNOWN, themes=[], classification_status='missing_source_identity')
        # Verified production source_type=3 is an ads_custom_source reference;
        # its own product + data_source_id identify the drama catalog.
        materials=self.reader.query(["""SELECT HEX(JSON_OBJECT('product',c.product,'content_id',c.data_source_id,
            'language',c.language,'data_source',c.data_source)) FROM kunlunads_dev.ads_source s
            JOIN kunlunads_dev.ads_custom_source c ON s.source_type=3 AND c.id=s.source_id
            WHERE s.id=%s LIMIT 2""" % literal(source)]) if source else []
        if len(materials)!=1 or materials[0].get('data_source')!=6:return result
        material=materials[0]
        source_app={'dramawave':1479,'freereels':979,'hotdrama':3302}.get(str(material.get('product','')).lower())
        if not source_app:return result
        cid=str(material['content_id']);lang=language(material.get('language'))
        rows=self.reader.query(["SELECT DISTINCT HEX(JSON_OBJECT('app_id',app_id,'content_id',content_id,'language',language,"
            "'labels',labels,'series_code',series_code)) FROM kunlunads_dev.ads_drama_resource "
            "WHERE app_id=%d AND content_id=%s LIMIT 100" % (source_app,literal(cid))])
        rows=[row for row in rows if str(row['content_id'])==cid and language(row.get('language'))==lang]
        identities = {(int(r['app_id']),str(r['content_id']),language(r.get('language'))) for r in rows}
        if len(identities)!=1:
            return result
        source_app,cid,lang = next(iter(identities))
        if source_app not in {1479,979,3302}:
            return result
        # An explicit source-resource link can legitimately be a DramaWave
        # material used in a HotDrama ad. It is not an ID-only cross-product match.
        infos = self.reader.query(["SELECT DISTINCT HEX(JSON_OBJECT('app_id',app_id,'content_id',content_id,'language',language,"
            "'series_code',series_code,'resource_type',resource_type)) FROM kunlunads_dev.ads_drama_info "
            "WHERE app_id=%d AND content_id=%s LIMIT 100" % (source_app,literal(cid))])
        shared(self.env)
        from features.fb_page_portrait.classification import classifications
        classification = classifications(rows,infos).get((source_app,cid,lang),{})
        return dict(language=lang or result['language'], audience=classification.get('audience',UNKNOWN),
                    themes=classification.get('themes',[]),source_app_id=source_app,content_id=cid,
                    classification_status=classification.get('mapping_status','unknown'))
