"""The report's fixed warehouse contract. Runtime never creates tables."""
import hashlib
import json

FACT_TABLES = {platform: 'social_analytics_' + platform + '_daily'
               for platform in ('youtube', 'x', 'facebook')}
CATALOG_TABLE = 'social_analytics_link_catalog'
PUBLICATION_TABLE = 'social_analytics_publication'
RUN_TABLE = 'social_analytics_refresh_runs'
TABLES = tuple(FACT_TABLES.values()) + (CATALOG_TABLE, PUBLICATION_TABLE, RUN_TABLE)
METRICS = ('clicks', 'views', 'installs', 'conversions', 'revenue_cents', 'refund_cents')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def fact_key(row):
    return digest([row['date'], row['site_id'], row['campaign_id']])


def ddl():
    statements = []
    for table in FACT_TABLES.values():
        statements.append('''CREATE TABLE IF NOT EXISTS ads_ai.%s (
          version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          dt DATE NOT NULL, site_id VARCHAR(16) NOT NULL,
          campaign_id VARBINARY(1024) NOT NULL,
          clicks BIGINT NOT NULL, views BIGINT NOT NULL, installs BIGINT NOT NULL,
          conversions BIGINT NOT NULL, revenue_cents BIGINT NOT NULL, refund_cents BIGINT NOT NULL,
          payload_json LONGTEXT NOT NULL,
          PRIMARY KEY(version,row_key), KEY day_version(dt,version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''' % table)
    for table in (CATALOG_TABLE, PUBLICATION_TABLE):
        statements.append('''CREATE TABLE IF NOT EXISTS ads_ai.%s (
          version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          platform VARCHAR(16) NOT NULL, site_id VARCHAR(16) NOT NULL,
          tenant VARCHAR(128) NOT NULL, owner VARCHAR(128) NOT NULL,
          dt DATE NULL, payload_json LONGTEXT NOT NULL,
          PRIMARY KEY(version,row_key), KEY owner_day(tenant,owner,dt)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''' % table)
    statements.append('''CREATE TABLE IF NOT EXISTS ads_ai.social_analytics_refresh_runs (
      version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
      status VARCHAR(16) NOT NULL, started_at VARCHAR(40) NOT NULL,
      finished_at VARCHAR(40) NOT NULL DEFAULT '', manifest_json LONGTEXT NOT NULL,
      error_code VARCHAR(128) NOT NULL DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''')
    return statements
