CREATE TABLE IF NOT EXISTS ads_ai.social_analytics_youtube_daily (
          version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          dt DATE NOT NULL, site_id VARCHAR(16) NOT NULL,
          campaign_id VARBINARY(1024) NOT NULL,
          clicks BIGINT NOT NULL, views BIGINT NOT NULL, installs BIGINT NOT NULL,
          conversions BIGINT NOT NULL, revenue_cents BIGINT NOT NULL, refund_cents BIGINT NOT NULL,
          payload_json LONGTEXT NOT NULL,
          PRIMARY KEY(version,row_key), KEY day_version(dt,version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_ai.social_analytics_x_daily (
          version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          dt DATE NOT NULL, site_id VARCHAR(16) NOT NULL,
          campaign_id VARBINARY(1024) NOT NULL,
          clicks BIGINT NOT NULL, views BIGINT NOT NULL, installs BIGINT NOT NULL,
          conversions BIGINT NOT NULL, revenue_cents BIGINT NOT NULL, refund_cents BIGINT NOT NULL,
          payload_json LONGTEXT NOT NULL,
          PRIMARY KEY(version,row_key), KEY day_version(dt,version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_ai.social_analytics_facebook_daily (
          version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          dt DATE NOT NULL, site_id VARCHAR(16) NOT NULL,
          campaign_id VARBINARY(1024) NOT NULL,
          clicks BIGINT NOT NULL, views BIGINT NOT NULL, installs BIGINT NOT NULL,
          conversions BIGINT NOT NULL, revenue_cents BIGINT NOT NULL, refund_cents BIGINT NOT NULL,
          payload_json LONGTEXT NOT NULL,
          PRIMARY KEY(version,row_key), KEY day_version(dt,version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_ai.social_analytics_link_catalog (
          version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          platform VARCHAR(16) NOT NULL, site_id VARCHAR(16) NOT NULL,
          tenant VARCHAR(128) NOT NULL, owner VARCHAR(128) NOT NULL,
          dt DATE NULL, payload_json LONGTEXT NOT NULL,
          PRIMARY KEY(version,row_key), KEY owner_day(tenant,owner,dt)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_ai.social_analytics_publication (
          version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          row_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          platform VARCHAR(16) NOT NULL, site_id VARCHAR(16) NOT NULL,
          tenant VARCHAR(128) NOT NULL, owner VARCHAR(128) NOT NULL,
          dt DATE NULL, payload_json LONGTEXT NOT NULL,
          PRIMARY KEY(version,row_key), KEY owner_day(tenant,owner,dt)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_ai.social_analytics_refresh_runs (
      version VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
      status VARCHAR(16) NOT NULL, started_at VARCHAR(40) NOT NULL,
      finished_at VARCHAR(40) NOT NULL DEFAULT '', manifest_json LONGTEXT NOT NULL,
      error_code VARCHAR(128) NOT NULL DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
