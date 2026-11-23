"""Reviewed DDL for initially empty shadow tables. Production DDL is ops-only."""
from .common import PUBLIC,NEXT,STATE,STAGE

HEAD_DDL=f"""CREATE TABLE IF NOT EXISTS {STATE} (
 singleton TINYINT UNSIGNED NOT NULL PRIMARY KEY,active_version VARCHAR(32) NOT NULL,
 window_start DATE NULL,window_end DATE NULL,page_count INT UNSIGNED NOT NULL,
 scope_sha256 CHAR(64) NOT NULL,manifest_sha256 CHAR(64) NOT NULL,
 paid_cents BIGINT NOT NULL,social_cents BIGINT NOT NULL,spend_cents BIGINT NOT NULL,
 meta_json LONGTEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
STAGE_DDL=f"""CREATE TABLE IF NOT EXISTS {STAGE} (
 profile_version VARCHAR(32) NOT NULL,page_id VARCHAR(32) NOT NULL,
 paid_cents BIGINT NOT NULL,social_cents BIGINT NOT NULL,spend_cents BIGINT NOT NULL,
 summary_json LONGTEXT NOT NULL,detail_json LONGTEXT NULL,payload_sha256 CHAR(64) NOT NULL,
 row_timestamp TIMESTAMP NOT NULL,PRIMARY KEY(profile_version,page_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def install(db,primary,exists):
    with db.cursor() as c:
        if not exists:c.execute(f'CREATE TABLE {NEXT} LIKE {PUBLIC}')
        if not exists or primary==('profile_version','page_id'):
            c.execute(f'ALTER TABLE {NEXT} DROP PRIMARY KEY, ADD PRIMARY KEY(page_id), ADD KEY version_page(profile_version,page_id)')
        elif primary!=('page_id',):
            raise RuntimeError('unexpected shadow primary key')
        c.execute(HEAD_DDL)
        c.execute(STAGE_DDL)
        c.execute(f"INSERT IGNORE INTO {STATE} VALUES(1,'',NULL,NULL,0,'','',0,0,0,'{{}}')")

