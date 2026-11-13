-- 部署前只读核验；不执行任何 DDL、写入或大表扫描。
-- 通过现有 Database(Settings)._read 或 /usr/bin/mysql 的 SQL FIFO 运行。
-- 客户端连接必须固定 101.32.56.53:63350、ads_ai，禁止直连驱动或绕开 FIFO。
-- 先检查 read_only=1、database=ads_ai；Database._read 已强制校验只读标志。
-- 密码仅由受限配置加载至 MYSQL_PWD，不写命令行、不打印配置或原始错误文本。
SELECT JSON_OBJECT('check','readonly_endpoint','read_only',@@read_only,'database',DATABASE());

-- 五张新表逐一返回；存在时必须先对照列、唯一索引和中文备注，不能用 IF NOT EXISTS 掩盖漂移。
SELECT JSON_OBJECT('check','new_table','table_name',expected.table_name,
                   'exists',IF(t.table_name IS NULL,0,1),'engine',t.engine,
                   'collation',t.table_collation,'comment',t.table_comment)
FROM (
  SELECT 'cdreader_koc_order' AS table_name
  UNION ALL SELECT 'cdreader_koc_ad_revenue'
  UNION ALL SELECT 'cdreader_koc_ad_day_snapshot'
  UNION ALL SELECT 'cdreader_koc_short_link'
  UNION ALL SELECT 'cdreader_koc_media_account'
) expected
LEFT JOIN information_schema.tables t ON t.table_schema='ads_ai' AND t.table_name=expected.table_name
ORDER BY expected.table_name;

-- 现有新表的类型、可空、默认值、字符集和中文备注，供与两份已审阅 DDL 比较。
SELECT JSON_OBJECT('check','column','table_name',table_name,'position',ordinal_position,
                   'column_name',column_name,'column_type',column_type,'nullable',is_nullable,
                   'default',column_default,'collation',collation_name,'extra',extra,'comment',column_comment)
FROM information_schema.columns
WHERE table_schema='ads_ai' AND table_name IN
 ('cdreader_koc_order','cdreader_koc_ad_revenue','cdreader_koc_ad_day_snapshot','cdreader_koc_short_link','cdreader_koc_media_account')
ORDER BY table_name,ordinal_position;

-- 原推广表本地 id 和组合关联字段；不查询用户、凭据、口令或链接内容。
SELECT JSON_OBJECT('check','promotion_reference_column','column_name',column_name,
                   'column_type',column_type,'nullable',is_nullable,'extra',extra)
FROM information_schema.columns
WHERE table_schema='ads_ai' AND table_name='cdreader_koc_promotion'
 AND column_name IN ('id','institution_user_id','star_user_id','project_type','story_id','request_id','koc_code')
ORDER BY ordinal_position;

-- 核对订单唯一键、广告版本键、有效日指针键、短码唯一键以及推广组合唯一键。
SELECT JSON_OBJECT('check','index','table_name',table_name,'index_name',index_name,
                   'non_unique',non_unique,'position',seq_in_index,'column_name',column_name,'prefix_length',sub_part)
FROM information_schema.statistics
WHERE table_schema='ads_ai' AND table_name IN
 ('cdreader_koc_order','cdreader_koc_ad_revenue','cdreader_koc_ad_day_snapshot','cdreader_koc_short_link','cdreader_koc_media_account','cdreader_koc_promotion')
ORDER BY table_name,index_name,seq_in_index;
