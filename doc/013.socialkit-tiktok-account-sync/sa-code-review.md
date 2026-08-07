# SA 代码评审

## 结论

通过本地代码评审，可以进入生产 dry-run 与受控部署。生产 DDL、首次同步和 timer 状态仍需按部署文档验证。

## 评审范围

- 同步脚本固定端点与事务边界。
- DDL、字段/索引和明文 Token 生命周期。
- systemd 沙箱、环境文件与小时计划。
- 自动测试和日志脱敏。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | 高 | sync 脚本 | 目标写前必须确认数据库与 read_only | 已加入窄 identity 查询 | 已修复 |
| CR-002 | 高 | sync 脚本 | 空源不能触发停用 | normalize 空源直接失败 | 已修复 |
| CR-003 | 高 | 日志 | 异常/summary 不能包含 Token | summary 仅计数，SQL 参数化 | 已修复 |
| CR-004 | 中 | systemd | ProtectSystem=strict 下锁文件需可写 | 仅开放 `/run/lock` | 已修复 |

## 编译 / 验证结果

- `python -m py_compile scripts\sync_socialkit_tiktok_accounts.py scripts\test_sync_socialkit_tiktok_accounts.py`：通过。
- `python scripts\test_sync_socialkit_tiktok_accounts.py`：15/15 通过。
- `git diff --cached --check`：通过。
- 变更集数据库密码扫描：未发现真实密码。
