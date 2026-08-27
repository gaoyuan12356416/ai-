-- Fixed CREATE-only SQL for the dedicated ads_ai drama YouTube ledger.
-- Apply only through bootstrap_drama_youtube_ads_ai.py after evidence validation.

CREATE TABLE ads_ai.ads_youtube_videos (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  publish_id BIGINT UNSIGNED NOT NULL,
  video_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  app_id INT UNSIGNED NOT NULL,
  channel_local_id INT UNSIGNED NOT NULL,
  operator_user_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  job_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  content_id VARCHAR(256) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  source_kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_url TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  title VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  description_rendered TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  privacy_status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  published_at_utc VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  canary_operation_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
  payload_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY ux_ds_video_external (video_id),
  UNIQUE KEY ux_ds_video_publish (publish_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET=utf8mb4 COLLATE=utf8mb4_bin ROW_FORMAT=DYNAMIC COMMENT='drama-synthesis:youtube-ledger:ads_ai:v2';

CREATE TABLE ads_ai.ads_youtube_comments (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  publish_id BIGINT UNSIGNED NOT NULL,
  video_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  comment_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  channel_local_id INT UNSIGNED NOT NULL,
  operator_user_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  comment_text TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  published_at_utc VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  canary_operation_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
  payload_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY ux_ds_comment_external (comment_id),
  UNIQUE KEY ux_ds_comment_publish (publish_id),
  UNIQUE KEY ux_ds_comment_video (video_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET=utf8mb4 COLLATE=utf8mb4_bin ROW_FORMAT=DYNAMIC COMMENT='drama-synthesis:youtube-ledger:ads_ai:v2';

CREATE TABLE ads_ai.ads_youtube_publish_log (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  publish_id BIGINT UNSIGNED NOT NULL,
  video_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  app_id INT UNSIGNED NOT NULL,
  channel_local_id INT UNSIGNED NOT NULL,
  operator_user_id VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  job_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  content_id VARCHAR(256) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  source_kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_url TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  title VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  description_rendered TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  privacy_status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  published_at_utc VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  canary_operation_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
  payload_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY ux_ds_log_publish (publish_id),
  UNIQUE KEY ux_ds_log_video (video_id)
) ENGINE=InnoDB DEFAULT CHARACTER SET=utf8mb4 COLLATE=utf8mb4_bin ROW_FORMAT=DYNAMIC COMMENT='drama-synthesis:youtube-ledger:ads_ai:v2';
