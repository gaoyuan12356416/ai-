-- 027.tt-post-direct-test-multi-config
-- IMPLEMENTATION MIRROR FOR REVIEW ONLY. DO NOT EXECUTE THIS FILE DIRECTLY.
-- The idempotent application migration lives in features/tt_posts/core.py.
-- This document intentionally contains only the two additive tables and
-- indexes that the current implementation creates. It does not introduce a
-- direct-test event table or a separate auto-due table.

CREATE TABLE IF NOT EXISTS tt_post_auto_publish_config (
    id INTEGER PRIMARY KEY CHECK(id=1),
    version INTEGER NOT NULL CHECK(version>0),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
        CHECK(timezone='Asia/Shanghai'),
    publish_times_json TEXT NOT NULL DEFAULT '[]',
    account_ids_json TEXT NOT NULL DEFAULT '[]',
    caption_template TEXT NOT NULL,
    user_consent INTEGER NOT NULL DEFAULT 0 CHECK(user_consent IN (0,1)),
    consent_version TEXT NOT NULL DEFAULT '',
    consented_at_utc TEXT NOT NULL DEFAULT '',
    created_by_user_id TEXT NOT NULL DEFAULT '',
    created_by_name TEXT NOT NULL DEFAULT '',
    updated_by_user_id TEXT NOT NULL DEFAULT '',
    updated_by_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tt_post_direct_test (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    material_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    source_media_url TEXT NOT NULL,
    prepared_media_url TEXT NOT NULL DEFAULT '',
    material_name TEXT NOT NULL DEFAULT '',
    drama_name TEXT NOT NULL DEFAULT '',
    material_language TEXT NOT NULL DEFAULT '',
    material_tag TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    account_username TEXT NOT NULL DEFAULT '',
    account_display_name TEXT NOT NULL DEFAULT '',
    creator_nickname_snapshot TEXT NOT NULL DEFAULT '',
    creator_username_snapshot TEXT NOT NULL DEFAULT '',
    creator_info_hash TEXT NOT NULL DEFAULT '',
    creator_info_synced_at_utc TEXT NOT NULL DEFAULT '',
    gpu_job_id TEXT NOT NULL UNIQUE,
    source_trim_tail_seconds REAL NOT NULL DEFAULT 0
        CHECK(source_trim_tail_seconds>=0),
    preparation_profile TEXT NOT NULL,
    prepared_output_sha256 TEXT NOT NULL DEFAULT '',
    prepared_output_size INTEGER NOT NULL DEFAULT 0
        CHECK(prepared_output_size>=0),
    prepared_duration_sec REAL NOT NULL DEFAULT 0
        CHECK(prepared_duration_sec>=0),
    caption_template TEXT NOT NULL,
    caption TEXT NOT NULL,
    short_link_id INTEGER NOT NULL DEFAULT 0 CHECK(short_link_id>=0),
    short_url TEXT NOT NULL DEFAULT '',
    long_url TEXT NOT NULL DEFAULT '',
    privacy_level TEXT NOT NULL CHECK(privacy_level IN (
        'PUBLIC_TO_EVERYONE',
        'MUTUAL_FOLLOW_FRIENDS',
        'FOLLOWER_OF_CREATOR',
        'SELF_ONLY'
    )),
    allow_comment INTEGER NOT NULL CHECK(allow_comment IN (0,1)),
    allow_duet INTEGER NOT NULL CHECK(allow_duet IN (0,1)),
    allow_stitch INTEGER NOT NULL CHECK(allow_stitch IN (0,1)),
    brand_content_toggle INTEGER NOT NULL CHECK(brand_content_toggle IN (0,1)),
    brand_organic_toggle INTEGER NOT NULL CHECK(brand_organic_toggle IN (0,1)),
    is_aigc INTEGER NOT NULL DEFAULT 0 CHECK(is_aigc IN (0,1)),
    user_consent INTEGER NOT NULL CHECK(user_consent=1),
    consent_version TEXT NOT NULL,
    consented_at_utc TEXT NOT NULL,
    config_version INTEGER NOT NULL DEFAULT 0 CHECK(config_version>=0),
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN (
        'queued','preparing','ready','publishing','reconciling',
        'published','failed','unknown','canceled'
    )),
    preparation_attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK(preparation_attempt_count>=0),
    publish_attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK(publish_attempt_count>=0),
    claim_phase TEXT NOT NULL DEFAULT '' CHECK(claim_phase IN ('','prepare','publish')),
    claim_worker TEXT NOT NULL DEFAULT '',
    claim_token TEXT NOT NULL DEFAULT '',
    lease_expires_at_utc TEXT NOT NULL DEFAULT '',
    publish_id TEXT NOT NULL DEFAULT '',
    publish_url TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    unknown_outcome INTEGER NOT NULL DEFAULT 0 CHECK(unknown_outcome IN (0,1)),
    created_by_user_id TEXT NOT NULL DEFAULT '',
    created_by_name TEXT NOT NULL DEFAULT '',
    updated_by_user_id TEXT NOT NULL DEFAULT '',
    updated_by_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_at_utc TEXT NOT NULL DEFAULT '',
    prepared_at_utc TEXT NOT NULL DEFAULT '',
    publish_started_at_utc TEXT NOT NULL DEFAULT '',
    published_at_utc TEXT NOT NULL DEFAULT '',
    failed_at_utc TEXT NOT NULL DEFAULT '',
    canceled_at_utc TEXT NOT NULL DEFAULT '',
    CHECK(
        (status='preparing' AND claim_phase='prepare')
        OR (status='publishing' AND claim_phase='publish')
        OR (status NOT IN ('preparing','publishing') AND claim_phase='')
    ),
    CHECK(
        (short_link_id=0 AND short_url='' AND long_url='')
        OR (short_link_id>0 AND short_url<>'')
    )
);

CREATE INDEX IF NOT EXISTS idx_tt_post_direct_test_prepare
    ON tt_post_direct_test(status,claim_phase,lease_expires_at_utc,created_at,id);
CREATE INDEX IF NOT EXISTS idx_tt_post_direct_test_publish
    ON tt_post_direct_test(status,claim_phase,lease_expires_at_utc,prepared_at_utc,id);
CREATE INDEX IF NOT EXISTS idx_tt_post_direct_test_material
    ON tt_post_direct_test(material_id,status,updated_at,id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_direct_test_publish_id
    ON tt_post_direct_test(publish_id) WHERE publish_id<>'';
CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_post_direct_test_short_link
    ON tt_post_direct_test(short_link_id) WHERE short_link_id>0;

-- Intentionally absent:
--   * tt_post_direct_test_event;
--   * tt_post_auto_due;
--   * any INSERT/UPDATE/DELETE/DROP against legacy schedule, pool, queue or run.
-- Same-minute durability reuses tt_post_schedule_run and
-- tt_post_recurring_pool through claim_recurring_run(). Legacy projection and
-- first-save schedule synchronization are application transactions.
