ALTER TABLE `ads_ai`.`fb_page_revenue_profile`
  MODIFY COLUMN `profile_version` varchar(32) NOT NULL COMMENT '画像批次号，同一次生成的所有主页共用一个版本',
  MODIFY COLUMN `page_id` varchar(32) NOT NULL COMMENT 'Facebook 主页 ID，每个主页在本表中只有一条当前画像',
  MODIFY COLUMN `window_start` date NOT NULL COMMENT '本次画像统计的第一天（UTC 日期，含当天）',
  MODIFY COLUMN `window_end` date NOT NULL COMMENT '本次画像统计的最后一天（UTC 日期，含当天），与开始日期合计 7 天',
  MODIFY COLUMN `paid_cents` bigint(20) NOT NULL COMMENT '统计期内归到该主页的广告投放收入（来源指标 af_revenue0，单位：美分）',
  MODIFY COLUMN `social_cents` bigint(20) NOT NULL COMMENT '统计期内归到该主页已确认发布的 Facebook 帖子的收入（单位：美分）',
  MODIFY COLUMN `spend_cents` bigint(20) NOT NULL COMMENT '统计期内归到该主页的广告投放花费（单位：美分）',
  MODIFY COLUMN `summary_json` longtext NOT NULL COMMENT '主页画像摘要，包括收入、花费、主要受众和题材（JSON）',
  MODIFY COLUMN `detail_json` longtext NOT NULL COMMENT '主页画像明细，可按受众、题材查看具体剧目及其收入（JSON）',
  MODIFY COLUMN `meta_json` longtext NOT NULL COMMENT '本批画像的统计说明，包括生成时间、统计周期、币种、数据来源和主页总数（JSON）',
  MODIFY COLUMN `payload_sha256` char(64) NOT NULL COMMENT '画像明细 detail_json 的 SHA-256 校验值，用于核对内容是否一致',
  MODIFY COLUMN `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '该主页当前这版画像的生成时间',
  ALGORITHM=INPLACE, LOCK=NONE;
