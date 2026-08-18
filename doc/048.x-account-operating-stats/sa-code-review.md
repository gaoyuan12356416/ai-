# SA 代码评审

## 结论

本地评审通过，可进入独立部署评审；不代表已上线。确认 `features/x_accounts/oauth_service.py` 无 diff。

| 编号 | 级别 | 问题 | 修复 | 状态 |
| --- | --- | --- | --- | --- |
| CR-001 | P0 | 初版 SQLite 测试留下 Windows 句柄 | reader/helper 显式 close | 已关闭 |
| CR-002 | P1 | 有效 cache 无历史账号初版显示 null | 有效 cache 缺行投影为 0 | 已关闭 |
| CR-003 | P1 | TO_BASE64 长 campaign 可能换行破坏 TSV | SQL 去 LF，精确 UTF-8 解码 | 已关闭 |
| CR-004 | P0 | 固定命令仍需防 mysql.real 绕过 | refresh 解析入口并拒绝 mysql.real | 已关闭 |

验证：新功能+app/UI 40/40，X accounts 68/68，ledger+random relay 27/27；py_compile、node、diff-check 通过。
