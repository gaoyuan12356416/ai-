-- Explicit deployment DDL only. Runtime never creates tables.
CREATE TABLE IF NOT EXISTS ads_ai.fb_auto_metric_generation (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  generation_key VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  platform INT NOT NULL,
  metric_date CHAR(10) CHARACTER SET ascii NOT NULL,
  product VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
  status VARCHAR(16) CHARACTER SET ascii NOT NULL,
  row_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
  checksum CHAR(64) CHARACTER SET ascii NOT NULL DEFAULT '',
  storage_checksum CHAR(64) CHARACTER SET ascii NOT NULL DEFAULT '',
  refreshed_at_utc VARCHAR(32) CHARACTER SET ascii NOT NULL,
  created_at_utc VARCHAR(32) CHARACTER SET ascii NOT NULL,
  ready_at_utc VARCHAR(32) CHARACTER SET ascii NOT NULL DEFAULT '',
  error_code VARCHAR(128) NOT NULL DEFAULT '',
  error_message VARCHAR(1024) NOT NULL DEFAULT '',
  PRIMARY KEY(id), UNIQUE KEY generation_key(generation_key),
  KEY scope_date(platform,product,metric_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ads_ai.fb_auto_metric_daily (
  generation_id BIGINT UNSIGNED NOT NULL,
  content_id VARCHAR(256) COLLATE utf8mb4_bin NOT NULL,
  material_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  spend DECIMAL(38,12) NOT NULL,
  af_revenue0 DECIMAL(38,12) NOT NULL,
  PRIMARY KEY(generation_id,content_id,material_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ads_ai.fb_auto_metric_active_pointer (
  platform INT NOT NULL,
  metric_date CHAR(10) CHARACTER SET ascii NOT NULL,
  product VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
  generation_id BIGINT UNSIGNED NOT NULL,
  activated_at_utc VARCHAR(32) CHARACTER SET ascii NOT NULL,
  refreshed_at_utc VARCHAR(32) CHARACTER SET ascii NOT NULL,
  PRIMARY KEY(platform,metric_date,product), KEY generation_id(generation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
