-- Existing-production migration for V3 multi-optimizer identity scope.
-- Run only against the reviewed ads_ai writer on port 63353.
-- The table is additive; rollback keeps it and its audit associations.

CREATE TABLE IF NOT EXISTS `ads_ai`.`ad_control_v3_rule_group_optimizer` (
  `rule_group_id` VARCHAR(64) NOT NULL,
  `optimizer_id` BIGINT UNSIGNED NOT NULL,
  `is_primary` TINYINT(1) NOT NULL DEFAULT 0,
  `created_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`rule_group_id`,`optimizer_id`),
  KEY `idx_v3_group_optimizer_reverse` (`optimizer_id`,`rule_group_id`),
  CONSTRAINT `fk_v3_group_optimizer_group`
    FOREIGN KEY (`rule_group_id`) REFERENCES `ads_ai`.`ad_control_v3_rule_group` (`group_id`)
    ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO `ads_ai`.`ad_control_v3_rule_group_optimizer`
  (`rule_group_id`,`optimizer_id`,`is_primary`,`created_at`)
SELECT `group_id`,`optimizer_id`,1,`created_at`
FROM `ads_ai`.`ad_control_v3_rule_group`;

SELECT COUNT(*) AS rule_group_count
FROM `ads_ai`.`ad_control_v3_rule_group`;

SELECT COUNT(DISTINCT `rule_group_id`) AS associated_group_count
FROM `ads_ai`.`ad_control_v3_rule_group_optimizer`;
