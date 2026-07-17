-- DESTRUCTIVE R1-only rollback. The procedure refuses to drop anything when
-- user configuration, previews, executions, targets, runner events or a
-- non-migration catalog row exists. Stop every V3 process before calling it.

DELIMITER //

DROP PROCEDURE IF EXISTS `ads_ai`.`rollback_empty_ad_control_v3`//
CREATE PROCEDURE `ads_ai`.`rollback_empty_ad_control_v3`()
BEGIN
  DECLARE business_rows BIGINT UNSIGNED DEFAULT 0;
  DECLARE custom_catalog_rows BIGINT UNSIGNED DEFAULT 0;

  SELECT
      (SELECT COUNT(*) FROM `ads_ai`.`ad_control_v3_rule_group`)
    + (SELECT COUNT(*) FROM `ads_ai`.`ad_control_v3_rule_group_optimizer`)
    + (SELECT COUNT(*) FROM `ads_ai`.`ad_control_v3_rule_group_product`)
    + (SELECT COUNT(*) FROM `ads_ai`.`ad_control_v3_preview`)
    + (SELECT COUNT(*) FROM `ads_ai`.`ad_control_v3_preview_target`)
    + (SELECT COUNT(*) FROM `ads_ai`.`ad_control_v3_execution`)
    + (SELECT COUNT(*) FROM `ads_ai`.`ad_control_v3_execution_target`)
    + (SELECT COUNT(*) FROM `ads_ai`.`ad_control_v3_runner_event`)
    INTO business_rows;

  SELECT COUNT(*) INTO custom_catalog_rows
  FROM `ads_ai`.`ad_control_v3_product_catalog`
  WHERE `created_by_user_id` <> 'migration'
     OR `updated_by_user_id` <> 'migration';

  IF business_rows <> 0 OR custom_catalog_rows <> 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT = 'ad_control_v3 rollback refused: real data exists';
  ELSE
    DELETE FROM `ads_ai`.`ad_control_v3_product_catalog`
    WHERE `created_by_user_id` = 'migration'
      AND `updated_by_user_id` = 'migration';

    DROP TABLE `ads_ai`.`ad_control_v3_runner_event`;
    DROP TABLE `ads_ai`.`ad_control_v3_execution_target`;
    DROP TABLE `ads_ai`.`ad_control_v3_execution`;
    DROP TABLE `ads_ai`.`ad_control_v3_preview_target`;
    DROP TABLE `ads_ai`.`ad_control_v3_preview`;
    DROP TABLE `ads_ai`.`ad_control_v3_rule_group_optimizer`;
    DROP TABLE `ads_ai`.`ad_control_v3_rule_group_product`;
    DROP TABLE `ads_ai`.`ad_control_v3_rule_group`;
    DROP TABLE `ads_ai`.`ad_control_v3_product_catalog`;
  END IF;
END//

CALL `ads_ai`.`rollback_empty_ad_control_v3`()//
DROP PROCEDURE `ads_ai`.`rollback_empty_ad_control_v3`//

DELIMITER ;
