"""Bounded writes to three explicitly owned ads_ai tables; all verification is read-only."""
import hashlib
import zlib
import time
from .store import encoded
from .model import METRICS, aggregate
from features.social_analytics.warehouse_refresh import Writer, Reader

POST = 'x_material_post_effect'
MATERIAL = 'x_material_effect'
RUN = 'x_material_effect_refresh_runs'
TABLES = (POST, MATERIAL)

def ddl():
    columns = ',\n'.join(k+' BIGINT NULL' for k in METRICS)
    common = f'''version VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
      row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
      namespace VARCHAR(32) NOT NULL,material_id VARCHAR(160) NOT NULL,
      product VARCHAR(64) NOT NULL, catalog VARCHAR(32) NOT NULL,
      content_id VARCHAR(128) NOT NULL,language VARCHAR(32) NOT NULL,
      {columns},payload_json LONGTEXT NOT NULL,
      PRIMARY KEY(version,row_key),KEY material_lookup(version,namespace,material_id),
      KEY drama_lookup(version,catalog,content_id,language,product)'''
    yield f'''CREATE TABLE IF NOT EXISTS ads_ai.{POST} ({common},
      post_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
      account_id VARCHAR(32) NOT NULL,published_at VARCHAR(32) NOT NULL,
      campaign_id VARCHAR(160) NOT NULL,KEY campaign_lookup(version,campaign_id),
      UNIQUE KEY post_identity(version,post_id),
      KEY account_date(version,account_id,published_at),
      KEY material_date(version,namespace,material_id,published_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''
    sorts=','.join('KEY sort_'+k+'(version,'+k+',row_key)' for k in ('views','likes','comments','shares','revenue_cents'))
    yield f'''CREATE TABLE IF NOT EXISTS ads_ai.{MATERIAL} ({common},
      post_count BIGINT NOT NULL,account_count BIGINT NOT NULL,{sorts}
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''
    yield f'''CREATE TABLE IF NOT EXISTS ads_ai.{RUN} (
      version VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
      status VARCHAR(16) NOT NULL,generated_at VARCHAR(32) NOT NULL,manifest_json LONGTEXT NOT NULL,
      KEY latest_verified(status,version)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''

class EffectWriter(Writer):
    def install_schema(self):
        with self.db.cursor() as cursor:
            for statement in ddl(): cursor.execute(statement)
        self.db.commit()

    def publish(self, version, posts, meta, reader):
        if len(version)>32 or not version.replace('T','').replace('Z','').isdigit(): raise ValueError('invalid_version')
        self.write_batch(f'INSERT INTO ads_ai.{RUN}(version,status,generated_at,manifest_json) VALUES(%s,%s,%s,%s)',
                         [(version,'collecting',meta['generated_at'],encoded(meta))])
        proof = {}
        for table, source in [(POST, posts),(MATERIAL, aggregate(posts))]:
            columns = ['version','row_key','namespace','material_id','product','catalog','content_id','language',*METRICS,'payload_json']
            extras = ['post_id','account_id','published_at','campaign_id'] if table == POST else ['post_count','account_count']
            columns += extras
            values=[]; crc=0; revenue=0
            for row in source:
                row = dict(row)
                if table == POST: row['campaign_id'] = (row.get('campaign_ids') or [''])[0]
                key = str(row['post_id']) if table==POST else encoded([row['namespace'],row['material_id']])
                payload=encoded(row);crc+=zlib.crc32(payload.encode());revenue+=row.get('revenue_cents') or 0
                values.append((version,hashlib.sha256(key.encode()).hexdigest(),
                    *(row.get(k,'') for k in columns[2:8]),*(row.get(k) for k in METRICS),payload,
                    *(row[k] for k in extras)))
            sql=f'INSERT INTO ads_ai.{table}('+','.join(columns)+') VALUES('+','.join('%s' for _ in columns)+') ON DUPLICATE KEY UPDATE payload_json=VALUES(payload_json)'
            for offset in range(0,len(values),1000):self.write_batch(sql,values[offset:offset+1000])
            proof[table]=dict(rows=len(source),crc=crc,revenue=revenue)
        statements=[f"SELECT HEX(JSON_OBJECT('table','{t}','rows',COUNT(*),'crc',COALESCE(SUM(CRC32(payload_json)),0),'revenue',COALESCE(SUM(revenue_cents),0))) FROM ads_ai.{t} WHERE version='{version}'" for t in TABLES]
        for attempt in range(4):
            checked=reader.query(statements,timeout=60)
            if len(checked)==2 and all(all(int(row[k])==proof[row['table']][k] for k in ('rows','crc','revenue')) for row in checked):break
            if attempt<3:time.sleep(3)
        for row in checked:
            if any(int(row[k])!=proof[row['table']][k] for k in ('rows','crc','revenue')):raise ValueError('effect_replica_verification_failed')
        if len(checked)!=2:raise ValueError('effect_replica_verification_missing')
        manifest=dict(meta,proof=proof)
        self.write_batch(f'UPDATE ads_ai.{RUN} SET status=%s,manifest_json=%s WHERE version=%s',[("verified",encoded(manifest),version)])
        # Keep two verified generations; scoped retention never touches source tables.
        old=reader.query([f"SELECT HEX(JSON_OBJECT('version',version)) FROM ads_ai.{RUN} WHERE status='verified' ORDER BY version DESC LIMIT 100 OFFSET 2"],timeout=30)
        for item in old:
            for table in TABLES:self.write_batch(f'DELETE FROM ads_ai.{table} WHERE version=%s',[(item['version'],)])
            self.write_batch(f'DELETE FROM ads_ai.{RUN} WHERE version=%s',[(item['version'],)])
        return manifest
