-- Run once by the reviewed deployment, never by hourly runtime.
CREATE TABLE IF NOT EXISTS ads_ai.x_channel_revenue_profile (
  profile_version VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  x_user_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  summary_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  tree_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  detail_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  revenue_cents BIGINT NOT NULL DEFAULT 0,
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (profile_version, x_user_id)
) ENGINE=InnoDB COMMENT='X channel revenue portraits; derived versioned projections';
