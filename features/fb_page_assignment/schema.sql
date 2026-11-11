-- Deployment only. Validate DATABASE()='ads_ai' and @@read_only=0 first.
-- Runtime never executes DDL. Reads for reporting use the 63350 replica.
CREATE TABLE IF NOT EXISTS `ads_ai`.`fb_drama_page_assignment_log` (
    `task_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    `item_index` SMALLINT UNSIGNED NOT NULL,
    `external_task_id` VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
    `pool_group_id` INT UNSIGNED NOT NULL,
    `drama_id` VARCHAR(128) COLLATE utf8mb4_bin NULL,
    `page_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    `status` VARCHAR(32) CHARACTER SET ascii NOT NULL,
    `selection_stage` VARCHAR(64) CHARACTER SET ascii NOT NULL,
    `error_code` VARCHAR(96) CHARACTER SET ascii NOT NULL,
    `error_message` VARCHAR(500) NOT NULL,
    `diagnostics_json` LONGTEXT NOT NULL,
    `http_status` SMALLINT UNSIGNED NOT NULL,
    `payload_hash` CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    `created_at` DATETIME(6) NOT NULL COMMENT 'UTC',
    `updated_at` DATETIME(6) NOT NULL COMMENT 'UTC',
    `revision` BIGINT UNSIGNED NOT NULL,
    `alert_status` VARCHAR(16) CHARACTER SET ascii NOT NULL,
    `alert_reasons_json` TEXT NOT NULL,
    `feishu_message_id` VARCHAR(128) CHARACTER SET ascii NOT NULL,
    `alert_error_code` VARCHAR(96) CHARACTER SET ascii NOT NULL,
    PRIMARY KEY (`task_id`, `item_index`),
    KEY `idx_fb_assignment_external` (`external_task_id`, `created_at`),
    KEY `idx_fb_assignment_drama` (`drama_id`, `created_at`),
    KEY `idx_fb_assignment_page` (`page_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
