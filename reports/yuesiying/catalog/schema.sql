CREATE TABLE IF NOT EXISTS ads_ai.report_material_catalog_generation (
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(16) CHARACTER SET ascii NOT NULL,
  source_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_kind VARCHAR(32) CHARACTER SET ascii NOT NULL,
  generated_at VARCHAR(40) NOT NULL,
  row_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
  row_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
  source_bytes BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  verified_at DATETIME(6) NULL,
  PRIMARY KEY (generation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ads_ai.report_material_catalog_row (
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_ordinal BIGINT UNSIGNED NOT NULL,
  material_id BIGINT UNSIGNED NOT NULL,
  content_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  language VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
  name MEDIUMTEXT NOT NULL,
  video_duration VARCHAR(64) CHARACTER SET ascii NOT NULL,
  source_created_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (generation_id, source_ordinal),
  UNIQUE KEY generation_material (generation_id, material_id),
  KEY generation_content (generation_id, content_id, source_ordinal)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ads_ai.report_material_catalog_pointer (
  report_key VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  previous_generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (report_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
