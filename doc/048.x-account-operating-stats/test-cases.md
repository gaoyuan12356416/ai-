# 测试用例

| 编号 | 场景 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| TC-001 | direct/relay Post actor | direct 归 target；relay 原 Post 归 relay actor | P0 | 通过 |
| TC-002 | Repost actor | 仅 reposted 归 target_account_id | P0 | 通过 |
| TC-003 | UTC→北京昨日边界 | 15:59:59Z/16:00:00Z 边界正确 | P0 | 通过 |
| TC-004 | 收入 SQL | 精确 site 2116、金额列、DB 日期定义 | P0 | 通过 |
| TC-005 | 精确 campaign | 唯一 c 匹配；重复/冲突不归属 | P0 | 通过 |
| TC-006 | 未归属 | missing/unmatched/conflict 汇总页顶，不分摊 | P0 | 通过 |
| TC-007 | USD | Decimal 6 位缓存，UI USD 两位 | P1 | 通过 |
| TC-008 | cache missing/stale | 列表不失败；缺失空值，过期旧值告警 | P0 | 通过 |
| TC-009 | UI | 六项存在；公众仅 followers/tweet/like | P0 | 通过 |
| TC-010 | auth/no-store | admin gate 与 no-store 保持 | P0 | 通过 |
| TC-011 | gate/secret | argv 仅 `/usr/bin/mysql`；密码仅 MYSQL_PWD | P0 | 通过 |
| TC-012 | timer/OAuth 回归 | 09:10/21:10；既有账号/relay tests 通过 | P0 | 通过 |

全部使用临时 SQLite、Decimal fixture 和 mocked subprocess；不连接生产 MySQL，不创建 X Post。
