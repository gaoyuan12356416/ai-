-- Reviewed deployment DDL only. Runtime code never executes DDL.
CREATE TABLE IF NOT EXISTS ads_ai.cdreader_koc_order (
  row_key CHAR(64) NOT NULL COMMENT '机构、短剧业务和外部订单号的确定性摘要',
  institution_user_id CHAR(32) NOT NULL COMMENT '畅读KOC机构用户ID',
  project_type TINYINT UNSIGNED NOT NULL COMMENT '请求业务类型，固定2短剧',
  ref_order_id VARCHAR(255) COLLATE utf8mb4_bin NOT NULL COMMENT '供应方外部订单号，同单状态变化更新原记录',
  source_create_time VARCHAR(64) NOT NULL COMMENT '供应方原始订单时间文本，时区未说明，不转换北京时间',
  source_date DATE DEFAULT NULL COMMENT '从有效供应方原始时间提取的来源日期，时区未说明',
  code VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '供应方返回的推广口令',
  story_id VARCHAR(64) NOT NULL COMMENT '上游短剧ID，保持字符串',
  story_name TEXT NOT NULL COMMENT '供应方返回的剧目名称',
  amount_usd DECIMAL(24,8) NOT NULL COMMENT '原始订单金额，单位美元，保留来源正负号',
  source_project_type TEXT NOT NULL COMMENT '供应方原始业务类型的JSON文本，兼容数字或中文枚举',
  source_status_text VARCHAR(255) NOT NULL COMMENT '供应方状态说明原文，不单独用来判定充值退款',
  coo_order_status INT DEFAULT NULL COMMENT '供应方数字订单状态，原文仅明确1为成功',
  coo_order_status_raw TEXT NOT NULL COMMENT '供应方订单状态原值的JSON文本，未知枚举保留',
  star_user_id VARCHAR(255) NOT NULL COMMENT '供应方达人登录账号ID，不等同我方达人ID',
  real_star_user_id VARCHAR(128) NOT NULL COMMENT '我方创建推广时传入的稳定达人ID',
  classification SMALLINT NOT NULL COMMENT '对账分类：0充值，1退款，-1未分类；必须按数值状态过滤结果交叉核对',
  classification_reason VARCHAR(64) NOT NULL COMMENT '分类或未分类原因标识',
  raw_json JSON NOT NULL COMMENT '完整原始订单字段，保留未知字段且不含认证凭据',
  first_seen_at DATETIME(3) NOT NULL COMMENT '首次入库时间，UTC',
  last_seen_at DATETIME(3) NOT NULL COMMENT '最近成功观测时间，UTC',
  last_sync_run_id CHAR(32) NOT NULL COMMENT '最近确认该记录的统计同步任务ID',
  PRIMARY KEY(row_key),
  UNIQUE KEY uk_order(institution_user_id,project_type,ref_order_id),
  KEY idx_order_date(institution_user_id,project_type,source_date),
  KEY idx_order_owner(real_star_user_id,source_date),
  KEY idx_order_code(code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Moboreels订单当前状态，历史观测保留于耐久快照';

CREATE TABLE IF NOT EXISTS ads_ai.cdreader_koc_ad_revenue (
  row_key CHAR(64) NOT NULL COMMENT '机构、业务、日期、口令和快照版本的确定性摘要',
  institution_user_id CHAR(32) NOT NULL COMMENT '畅读KOC机构用户ID',
  project_type TINYINT UNSIGNED NOT NULL COMMENT '业务类型，固定2短剧',
  snapshot_id CHAR(32) NOT NULL COMMENT '完整广告日快照所属同步任务ID，历史版本不删除',
  ad_time DATE NOT NULL COMMENT '供应方广告收益日期，统计时区未说明',
  code VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '推广口令，同日同版本按口令唯一',
  amount_cents DECIMAL(38,12) NOT NULL COMMENT '原始广告收益，单位美分，保留小数美分',
  amount_usd DECIMAL(38,14) NOT NULL COMMENT '广告收益美元金额，原始美分精确除以100',
  raw_json JSON NOT NULL COMMENT '原始广告收益字段，未知字段保留且不含认证凭据',
  first_seen_at DATETIME(3) NOT NULL COMMENT '版本首次入库时间，UTC',
  last_seen_at DATETIME(3) NOT NULL COMMENT '版本最近核对时间，UTC',
  last_sync_run_id CHAR(32) NOT NULL COMMENT '最近核对该版本的统计同步任务ID',
  PRIMARY KEY(row_key),
  UNIQUE KEY uk_ad_version(institution_user_id,project_type,ad_time,code,snapshot_id),
  KEY idx_ad_day(institution_user_id,project_type,ad_time,snapshot_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Moboreels广告收益不可变日版本，不按多个短链重复累计';

CREATE TABLE IF NOT EXISTS ads_ai.cdreader_koc_ad_day_snapshot (
  institution_user_id CHAR(32) NOT NULL COMMENT '畅读KOC机构用户ID',
  project_type TINYINT UNSIGNED NOT NULL COMMENT '业务类型，固定2短剧',
  ad_time DATE NOT NULL COMMENT '供应方统计日期，时区未说明',
  snapshot_id CHAR(32) NOT NULL COMMENT '当前完整取数、写入和读回核验通过的日版本ID',
  row_count BIGINT UNSIGNED NOT NULL COMMENT '完整快照记录数，0表示明确空日',
  source_hash CHAR(64) NOT NULL COMMENT '完整规范化日快照摘要',
  completed_at DATETIME(3) NOT NULL COMMENT '快照切换核验时间，UTC',
  PRIMARY KEY(institution_user_id,project_type,ad_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='广告报表当前有效日版本；空日切换后不再展示历史收入';
