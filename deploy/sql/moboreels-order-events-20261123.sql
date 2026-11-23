-- Scoped deployment only: preserve every row and the idempotent row_key primary key.
-- Recharge retains its legacy key; refund uses a separate deterministic key.
ALTER TABLE ads_ai.cdreader_koc_order
  DROP INDEX uk_order,
  ADD KEY idx_order_ref(institution_user_id,project_type,ref_order_id),
  MODIFY row_key CHAR(64) NOT NULL COMMENT '机构、短剧业务、外部订单号和充值退款事件的确定性摘要',
  MODIFY ref_order_id VARCHAR(255) COLLATE utf8mb4_bin NOT NULL COMMENT '供应方外部订单号，充值和退款分别保留',
  COMMENT='Moboreels充值退款事件，历史观测保留于耐久快照';
