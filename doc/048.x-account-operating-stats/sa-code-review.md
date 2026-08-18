# SA 代码评审

## 结论

本地评审通过，可进入独立部署评审；不代表已上线。确认 `features/x_accounts/oauth_service.py` 无 diff。

| 编号 | 级别 | 问题 | 修复 | 状态 |
| --- | --- | --- | --- | --- |
| CR-001 | P0 | 初版 SQLite 测试留下 Windows 句柄 | reader/helper 显式 close | 已关闭 |
| CR-002 | P1 | 有效 cache 无历史账号初版显示 null | 有效 cache 缺行投影为 0 | 已关闭 |
| CR-003 | P1 | TO_BASE64 长 campaign 可能换行破坏 TSV | SQL 去 LF，精确 UTF-8 解码 | 已关闭 |
| CR-004 | P0 | 固定命令仍需防 mysql.real 绕过 | refresh 解析入口并拒绝 mysql.real | 已关闭 |
| CR-005 | P1 | relay actor 初版取 ledger.relay_account_id | JOIN queue，校验冻结 target/relay/mode，一律取 q.relay_account_id | 已关闭 |
| CR-006 | P1 | gate 初版只按 basename 拒绝 mysql.real | 精确要求 `/usr/local/bin/mysql-gated`，其他目标全部拒绝 | 已关闭 |
| CR-007 | P2 | cache 初版只有 age 判定 | 加北京 business_date 与 5 分钟 future skew，UI 明示 yesterday_date | 已关闭 |
| CR-008 | P1 | 收入初版误用列 `c` | 改为真实 `campaign`，并 FORCE INDEX | 已关闭 |
| CR-009 | P1 | failed/reserved log 初版参与 campaign map | 仅 confirmed published log 建映射 | 已关闭 |

最终验证结果以 test-report 为准；禁改 `oauth_service.py` 持续零 diff。
