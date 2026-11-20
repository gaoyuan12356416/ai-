CREATE TABLE IF NOT EXISTS ads_ai.report_history_snapshot (
    snapshot_id BINARY(32) NOT NULL,
    dataset VARCHAR(40) CHARACTER SET ascii NOT NULL,
    relative_path VARCHAR(768) NOT NULL,
    source_sha256 BINARY(32) NOT NULL,
    source_bytes BIGINT UNSIGNED NOT NULL,
    source_mtime BIGINT NOT NULL,
    source_format VARCHAR(16) CHARACTER SET ascii NOT NULL,
    columns_json JSON NOT NULL,
    document_metadata JSON NOT NULL,
    page_ids JSON NOT NULL,
    status ENUM('staging','verified') NOT NULL,
    record_count BIGINT UNSIGNED NOT NULL,
    page_count INT UNSIGNED NOT NULL,
    records_sha256 BINARY(32) NOT NULL,
    verified_at DATETIME(6) NULL,
    PRIMARY KEY (snapshot_id),
    KEY history_dataset_time (dataset, source_mtime),
    KEY history_source_hash (source_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ads_ai.report_history_metric_page (
    page_id BINARY(32) NOT NULL,
    dataset VARCHAR(40) CHARACTER SET ascii NOT NULL,
    record_count INT UNSIGNED NOT NULL,
    min_date DATE NULL,
    max_date DATE NULL,
    spend_sum DECIMAL(38,8) NULL,
    revenue_sum DECIMAL(38,8) NULL,
    installs_sum DECIMAL(38,8) NULL,
    impressions_sum DECIMAL(38,8) NULL,
    clicks_sum DECIMAL(38,8) NULL,
    records_sha256 BINARY(32) NOT NULL,
    rows_json JSON NOT NULL,
    PRIMARY KEY (page_id),
    KEY history_page_dates (min_date, max_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
