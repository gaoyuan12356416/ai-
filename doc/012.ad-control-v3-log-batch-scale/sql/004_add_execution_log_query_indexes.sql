-- V3 execution-log read-path indexes.
-- MySQL 5.7 compatible. Apply through the ads_ai writer role only after
-- confirming that none of these index names already exists.

ALTER TABLE `ads_ai`.`ad_control_v3_execution`
  ADD KEY `idx_v3_execution_created` (`created_at`,`execution_id`),
  ADD KEY `idx_v3_execution_preview_mode` (`preview_id`,`run_mode`,`trigger_source`),
  ALGORITHM=INPLACE,
  LOCK=NONE;

ALTER TABLE `ads_ai`.`ad_control_v3_execution_target`
  ADD KEY `idx_v3_execution_target_action_lookup` (`execution_id`,`action`),
  ADD KEY `idx_v3_execution_target_object_lookup` (`execution_id`,`object_id`),
  ADD KEY `idx_v3_execution_target_product_lookup` (`execution_id`,`product_value`),
  ALGORITHM=INPLACE,
  LOCK=NONE;
