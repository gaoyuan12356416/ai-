CREATE TABLE IF NOT EXISTS ads_ai.fb_page_effect_profile (
  profile_version VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  group_id BIGINT NOT NULL,
  page_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  language VARCHAR(32) NOT NULL,
  window_start DATE NULL,
  window_end DATE NULL,
  window_kind VARCHAR(24) NOT NULL,
  spend_cents BIGINT NOT NULL,
  revenue_d0_cents BIGINT NOT NULL,
  summary_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  profile_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (profile_version, page_id),
  KEY group_language (group_id, language, profile_version)
) ENGINE=InnoDB COMMENT='Scoped Facebook Page paid-effect portraits; immutable snapshots';
