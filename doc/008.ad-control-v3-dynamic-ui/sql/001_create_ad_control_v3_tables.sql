-- V3 only. MySQL 5.7 compatible. Never run from application startup.
-- Reviewed write database: ads_ai. This migration does not touch V2 SQLite or tables.

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_product_catalog` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `channel` VARCHAR(32) NOT NULL,
  `product_value` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `canonical_product` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `product_type` VARCHAR(32) NOT NULL,
  `source_app_ids_json` JSON NOT NULL,
  `evidence_json` JSON NOT NULL,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `created_by_user_id` VARCHAR(128) NOT NULL DEFAULT '',
  `updated_by_user_id` VARCHAR(128) NOT NULL DEFAULT '',
  `created_at` DATETIME(6) NOT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_v3_product_channel_value` (`channel`,`product_value`),
  KEY `idx_v3_product_enabled_type` (`channel`,`enabled`,`product_type`,`product_value`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_rule_group` (
  `group_id` VARCHAR(64) NOT NULL,
  `name` VARCHAR(128) NOT NULL,
  `description` VARCHAR(1000) NOT NULL DEFAULT '',
  `channel` VARCHAR(32) NOT NULL,
  `object_level` VARCHAR(16) NOT NULL,
  `run_mode` VARCHAR(16) NOT NULL,
  `owner_user_id` VARCHAR(128) NOT NULL,
  `optimizer_id` BIGINT UNSIGNED NOT NULL,
  `account_timezones_json` JSON NOT NULL,
  `rules_json` JSON NOT NULL,
  `schedule_json` JSON NOT NULL,
  `quotas_json` JSON NOT NULL,
  `selection_json` JSON NOT NULL,
  `behavior_hash` CHAR(64) NOT NULL,
  `config_version` INT UNSIGNED NOT NULL,
  `last_preview_id` VARCHAR(64) NOT NULL DEFAULT '',
  `last_preview_hash` CHAR(64) NOT NULL DEFAULT '',
  `enabled` TINYINT(1) NOT NULL DEFAULT 0,
  `emergency_stopped` TINYINT(1) NOT NULL DEFAULT 0,
  `deleted` TINYINT(1) NOT NULL DEFAULT 0,
  `created_by_user_id` VARCHAR(128) NOT NULL,
  `updated_by_user_id` VARCHAR(128) NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`group_id`),
  KEY `idx_v3_group_optimizer_list` (`optimizer_id`,`deleted`,`updated_at`),
  KEY `idx_v3_group_runner` (`enabled`,`emergency_stopped`,`deleted`,`channel`,`run_mode`),
  KEY `idx_v3_group_owner` (`owner_user_id`,`deleted`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_rule_group_product` (
  `rule_group_id` VARCHAR(64) NOT NULL,
  `product_value` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`rule_group_id`,`product_value`),
  KEY `idx_v3_group_product_reverse` (`product_value`,`rule_group_id`),
  CONSTRAINT `fk_v3_group_product_group`
    FOREIGN KEY (`rule_group_id`) REFERENCES `ads_ai`.`ad_control_v3_rule_group` (`group_id`)
    ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_preview` (
  `preview_id` VARCHAR(64) NOT NULL,
  `rule_group_id` VARCHAR(64) NOT NULL,
  `config_version` INT UNSIGNED NOT NULL,
  `behavior_hash` CHAR(64) NOT NULL,
  `optimizer_id` BIGINT UNSIGNED NOT NULL,
  `channel` VARCHAR(32) NOT NULL,
  `object_level` VARCHAR(16) NOT NULL,
  `status` VARCHAR(32) NOT NULL,
  `summary_json` JSON NOT NULL,
  `snapshot_relative_path` VARCHAR(512) NOT NULL,
  `snapshot_sha256` CHAR(64) NOT NULL,
  `snapshot_byte_size` BIGINT UNSIGNED NOT NULL,
  `created_by_user_id` VARCHAR(128) NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  `expires_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`preview_id`),
  KEY `idx_v3_preview_group_created` (`rule_group_id`,`created_at`),
  KEY `idx_v3_preview_optimizer_created` (`optimizer_id`,`created_at`),
  CONSTRAINT `fk_v3_preview_group`
    FOREIGN KEY (`rule_group_id`) REFERENCES `ads_ai`.`ad_control_v3_rule_group` (`group_id`)
    ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_preview_target` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `preview_id` VARCHAR(64) NOT NULL,
  `target_no` INT UNSIGNED NOT NULL,
  `ad_account_id` VARCHAR(64) NOT NULL,
  `object_level` VARCHAR(16) NOT NULL,
  `object_id` VARCHAR(64) NOT NULL,
  `campaign_id` VARCHAR(64) NOT NULL DEFAULT '',
  `adset_id` VARCHAR(64) NOT NULL DEFAULT '',
  `ad_id` VARCHAR(64) NOT NULL DEFAULT '',
  `product_value` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `optimizer_id` BIGINT UNSIGNED NOT NULL,
  `action` VARCHAR(16) NOT NULL DEFAULT '',
  `control_rule_id` VARCHAR(64) NOT NULL DEFAULT '',
  `status` VARCHAR(32) NOT NULL,
  `reason` VARCHAR(64) NOT NULL DEFAULT '',
  `detail_json` JSON NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_v3_preview_target_no` (`preview_id`,`target_no`),
  UNIQUE KEY `uk_v3_preview_object` (`preview_id`,`ad_account_id`,`object_level`,`object_id`),
  KEY `idx_v3_preview_target_result` (`preview_id`,`status`,`action`),
  CONSTRAINT `fk_v3_preview_target_preview`
    FOREIGN KEY (`preview_id`) REFERENCES `ads_ai`.`ad_control_v3_preview` (`preview_id`)
    ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_execution` (
  `execution_id` VARCHAR(64) NOT NULL,
  `rule_group_id` VARCHAR(64) NOT NULL,
  `preview_id` VARCHAR(64) NOT NULL DEFAULT '',
  `config_version` INT UNSIGNED NOT NULL,
  `behavior_hash` CHAR(64) NOT NULL,
  `optimizer_id` BIGINT UNSIGNED NOT NULL,
  `channel` VARCHAR(32) NOT NULL,
  `object_level` VARCHAR(16) NOT NULL,
  `run_mode` VARCHAR(16) NOT NULL,
  `trigger_source` VARCHAR(32) NOT NULL,
  `status` VARCHAR(32) NOT NULL,
  `summary_json` JSON NOT NULL,
  `snapshot_relative_path` VARCHAR(512) NOT NULL,
  `snapshot_sha256` CHAR(64) NOT NULL,
  `snapshot_byte_size` BIGINT UNSIGNED NOT NULL,
  `created_by_user_id` VARCHAR(128) NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  `finished_at` DATETIME(6) NULL,
  PRIMARY KEY (`execution_id`),
  KEY `idx_v3_execution_created` (`created_at`,`execution_id`),
  KEY `idx_v3_execution_preview_mode` (`preview_id`,`run_mode`,`trigger_source`),
  KEY `idx_v3_execution_optimizer_created` (`optimizer_id`,`created_at`),
  KEY `idx_v3_execution_group_created` (`rule_group_id`,`created_at`),
  KEY `idx_v3_execution_filter` (`channel`,`object_level`,`run_mode`,`status`,`created_at`),
  CONSTRAINT `fk_v3_execution_group`
    FOREIGN KEY (`rule_group_id`) REFERENCES `ads_ai`.`ad_control_v3_rule_group` (`group_id`)
    ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_execution_target` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `execution_id` VARCHAR(64) NOT NULL,
  `target_no` INT UNSIGNED NOT NULL,
  `ad_account_id` VARCHAR(64) NOT NULL,
  `object_level` VARCHAR(16) NOT NULL,
  `object_id` VARCHAR(64) NOT NULL,
  `campaign_id` VARCHAR(64) NOT NULL DEFAULT '',
  `adset_id` VARCHAR(64) NOT NULL DEFAULT '',
  `ad_id` VARCHAR(64) NOT NULL DEFAULT '',
  `product_value` VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `optimizer_id` BIGINT UNSIGNED NOT NULL,
  `action` VARCHAR(16) NOT NULL DEFAULT '',
  `control_rule_id` VARCHAR(64) NOT NULL DEFAULT '',
  `status` VARCHAR(32) NOT NULL,
  `reason` VARCHAR(64) NOT NULL DEFAULT '',
  `detail_json` JSON NOT NULL,
  `created_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_v3_execution_target_no` (`execution_id`,`target_no`),
  UNIQUE KEY `uk_v3_execution_object` (`execution_id`,`ad_account_id`,`object_level`,`object_id`),
  KEY `idx_v3_execution_target_result` (`execution_id`,`status`,`action`),
  KEY `idx_v3_execution_target_action_lookup` (`execution_id`,`action`),
  KEY `idx_v3_execution_target_object_lookup` (`execution_id`,`object_id`),
  KEY `idx_v3_execution_target_product_lookup` (`execution_id`,`product_value`),
  KEY `idx_v3_execution_target_product` (`product_value`,`optimizer_id`,`created_at`),
  CONSTRAINT `fk_v3_execution_target_execution`
    FOREIGN KEY (`execution_id`) REFERENCES `ads_ai`.`ad_control_v3_execution` (`execution_id`)
    ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_runner_event` (
  `event_key` VARCHAR(128) NOT NULL,
  `rule_group_id` VARCHAR(64) NOT NULL,
  `scheduled_for` DATETIME(6) NOT NULL,
  `status` VARCHAR(32) NOT NULL,
  `lease_owner` VARCHAR(128) NOT NULL,
  `lease_expires_at` DATETIME(6) NOT NULL,
  `attempt_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `execution_id` VARCHAR(64) NOT NULL DEFAULT '',
  `error_code` VARCHAR(64) NOT NULL DEFAULT '',
  `error_message` VARCHAR(1000) NOT NULL DEFAULT '',
  `created_at` DATETIME(6) NOT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`event_key`),
  KEY `idx_v3_runner_due` (`status`,`lease_expires_at`),
  KEY `idx_v3_runner_group_time` (`rule_group_id`,`scheduled_for`),
  CONSTRAINT `fk_v3_runner_group`
    FOREIGN KEY (`rule_group_id`) REFERENCES `ads_ai`.`ad_control_v3_rule_group` (`group_id`)
    ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
