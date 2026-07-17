-- Reviewed production migration for V3 Facebook pause/copy.
-- MySQL 5.7 compatible. Apply manually through the ads_ai writer role only.
-- The CREATE ... LIKE statement is followed by an application-side schema
-- signature check before every copy; drift immediately blocks Meta writes.

CREATE TABLE IF NOT EXISTS `ads_ai`.`ads_facebook_auto_created_data`
LIKE `kunlunads_dev`.`ads_facebook_auto_created_data`;

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_copy_intent` (
  `intent_id` VARCHAR(64) NOT NULL,
  `idempotency_key` CHAR(64) NOT NULL,
  `owner_user_id` VARCHAR(128) NOT NULL,
  `rule_group_id` VARCHAR(64) NOT NULL,
  `control_rule_id` VARCHAR(64) NOT NULL,
  `optimizer_id` BIGINT UNSIGNED NOT NULL,
  `ad_account_id` VARCHAR(64) NOT NULL,
  `object_level` VARCHAR(16) NOT NULL,
  `source_object_id` VARCHAR(64) NOT NULL,
  `account_date` DATE NOT NULL,
  `behavior_hash` CHAR(64) NOT NULL,
  `status` VARCHAR(32) NOT NULL,
  `result_json` JSON NOT NULL,
  `error_code` VARCHAR(64) NOT NULL DEFAULT '',
  `error_message` VARCHAR(1000) NOT NULL DEFAULT '',
  `created_at` DATETIME(6) NOT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  `completed_at` DATETIME(6) NULL,
  PRIMARY KEY (`intent_id`),
  UNIQUE KEY `uk_v3_copy_intent_idempotency` (`idempotency_key`),
  KEY `idx_v3_copy_owner_date` (`owner_user_id`,`account_date`,`status`),
  KEY `idx_v3_copy_group_date` (`rule_group_id`,`account_date`,`status`),
  KEY `idx_v3_copy_rule_date` (`rule_group_id`,`control_rule_id`,`account_date`,`status`),
  KEY `idx_v3_copy_source_cooldown` (`ad_account_id`,`object_level`,`source_object_id`,`created_at`,`status`),
  CONSTRAINT `fk_v3_copy_intent_group`
    FOREIGN KEY (`rule_group_id`) REFERENCES `ads_ai`.`ad_control_v3_rule_group` (`group_id`)
    ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_copy_lineage` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `channel` VARCHAR(32) NOT NULL,
  `copy_intent_id` VARCHAR(64) NOT NULL,
  `source_database` VARCHAR(64) NOT NULL,
  `source_table` VARCHAR(128) NOT NULL,
  `source_created_data_id` BIGINT UNSIGNED NOT NULL,
  `source_campaign_id` VARCHAR(64) NOT NULL,
  `source_adset_id` VARCHAR(64) NOT NULL,
  `source_ad_id` VARCHAR(64) NOT NULL,
  `new_created_data_id` BIGINT UNSIGNED NOT NULL,
  `new_campaign_id` VARCHAR(64) NOT NULL,
  `new_adset_id` VARCHAR(64) NOT NULL,
  `new_ad_id` VARCHAR(64) NOT NULL,
  `new_creative_id` VARCHAR(64) NOT NULL,
  `rule_group_id` VARCHAR(64) NOT NULL,
  `control_rule_id` VARCHAR(64) NOT NULL,
  `owner_user_id` VARCHAR(128) NOT NULL,
  `optimizer_id` BIGINT UNSIGNED NOT NULL,
  `meta_status` VARCHAR(32) NOT NULL,
  `ledger_status` VARCHAR(32) NOT NULL,
  `activation_status` VARCHAR(32) NOT NULL,
  `error_reason` VARCHAR(1000) NOT NULL DEFAULT '',
  `created_at` DATETIME(6) NOT NULL,
  `updated_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ad_control_lineage_new_ad` (`channel`,`new_ad_id`),
  UNIQUE KEY `uk_ad_control_lineage_created_data` (`channel`,`new_created_data_id`),
  KEY `idx_ad_control_lineage_intent` (`copy_intent_id`,`id`),
  KEY `idx_ad_control_lineage_source` (`channel`,`source_campaign_id`,`source_adset_id`,`source_ad_id`),
  KEY `idx_ad_control_lineage_group` (`rule_group_id`,`control_rule_id`,`created_at`),
  CONSTRAINT `fk_ad_control_lineage_intent`
    FOREIGN KEY (`copy_intent_id`) REFERENCES `ads_ai`.`ad_control_v3_copy_intent` (`intent_id`)
    ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
