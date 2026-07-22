# SA 代码评审

## 结论

通过代码评审与生产验收，已完成受控部署。运行时固定写入 63353/ads_ai，常规验收从 63350 只读回查；主 API 无需重启。

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
- `python scripts\test_sync_socialkit_tiktok_accounts.py`：13/13 通过。
- `git diff --cached --check`：通过。
- 变更集数据库密码扫描：未发现真实密码。
- 精确 commit release 上复验编译、13 个单元测试、unit verify：通过。
- 两次生产同步、源目标逐字段对账、timer 状态、日志/发布目录敏感值扫描：通过。
- 生产 env 为 root:root 0600；inactive Token 泄漏计数为 0。
