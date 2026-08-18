# 测试用例

| 编号 | 场景 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| TC-001 | direct/relay Post actor | direct 归 target；relay 原 Post 归 relay actor | P0 | 通过 |
| TC-002 | Repost actor | 仅 reposted 归 target_account_id | P0 | 通过 |
| TC-003 | UTC→北京昨日边界 | 15:59:59Z/16:00:00Z 边界正确 | P0 | 通过 |
| TC-004 | 收入 SQL | 精确 site 2116、金额列、DB 日期定义 | P0 | 通过 |
| TC-005 | 精确 campaign | 唯一 W2A `c` 匹配；重复/冲突不归属 | P0 | 通过 |
| TC-006 | 未归属 | missing/unmatched/conflict 汇总页顶，不分摊 | P0 | 通过 |
| TC-007 | USD | Decimal 6 位缓存，UI USD 两位 | P1 | 通过 |
| TC-008 | cache missing/stale | 列表不失败；缺失空值，过期旧值告警 | P0 | 通过 |
| TC-009 | UI | 六项存在；公众仅 followers/tweet/like | P0 | 通过 |
| TC-010 | auth/no-store | admin gate 与 no-store 保持 | P0 | 通过 |
| TC-011 | gate/secret | argv 仅 `/usr/bin/mysql`；密码仅 MYSQL_PWD | P0 | 通过 |
| TC-012 | timer/OAuth 回归 | 09:10/21:10；既有账号/relay tests 通过 | P0 | 通过 |
| TC-013 | relay ledger 对抗冲突 | ledger relay 与 queue 冻结 relay 不同 | P0 | 通过：记录 conflict，双方均不计数 |
| TC-014 | gate 漂移 | mysql.real、mariadb、其他 binary/path | P0 | 通过：均 fail closed |
| TC-015 | cache 跨日/未来 | TTL 内跨北京日、未来 10 分钟、未来 2 分钟 | P0 | 通过：前两者 stale，允许 2 分钟偏差 |
| TC-016 | 收入 schema/index | `campaign` 列、强制 idx_site_event_time、不把 W2A `c` 当作表列 | P0 | 通过 |
| TC-017 | 未确认 log | failed log 与已发布 log 使用同一 W2A c | P0 | 通过：只采用已发布账号 |

全部使用临时 SQLite、Decimal fixture 和 mocked subprocess；不连接生产 MySQL，不创建 X Post。UI 明示 `business_date/yesterday_date`。
