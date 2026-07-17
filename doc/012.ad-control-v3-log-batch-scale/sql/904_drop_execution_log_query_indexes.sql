-- DDL-only rollback. This does not delete any execution or target rows.

ALTER TABLE `ads_ai`.`ad_control_v3_execution`
  DROP KEY `idx_v3_execution_created`,
  DROP KEY `idx_v3_execution_preview_mode`,
  ALGORITHM=INPLACE,
  LOCK=NONE;

ALTER TABLE `ads_ai`.`ad_control_v3_execution_target`
  DROP KEY `idx_v3_execution_target_action_lookup`,
  DROP KEY `idx_v3_execution_target_object_lookup`,
  DROP KEY `idx_v3_execution_target_product_lookup`,
  ALGORITHM=INPLACE,
  LOCK=NONE;
