# 测试报告

## 测试结论

本地与生产验证均通过，已完成受控发布。目标表当前与源表逐字段一致，小时 timer 已启用。

## 测试范围

单元测试、Python 语法、DDL 静态检查、systemd unit 校验、生产 dry-run、首次/幂等同步、63350 脱敏对账与 timer 状态。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 单元测试 | 13 | 13 | 0 | 0 |
| 本地静态检查 | 3 | 3 | 0 | 0 |
| 生产验证 | 12 | 12 | 0 | 0 |

## 缺陷情况

暂无未关闭缺陷。DDL 首次尝试的前置身份查询使用了保留字别名，查询在任何 DDL 执行前失败；修正别名后成功，不影响数据或服务。

## 验证证据

- `py_compile`：通过。
- `scripts/test_sync_socialkit_tiktok_accounts.py`：`Ran 13 tests ... OK`。
- `git diff --cached --check`：通过。
- 变更集未发现真实数据库密码。
- release 上执行 Python 编译、13 个单元测试和 systemd unit verify：全部通过。
- 生产 dry-run：源活动账号 24、指标快照 23、缺指标 1、非空 Token 24。
- 首次同步与第二次幂等同步：均成功，第二次执行后总行数仍为 24。
- 源/目标逐字段对账：`missing_target_ids=0`、`extra_target_ids=0`、`field_mismatches={}`。
- 63350 读库回查：24 个活动账号、0 个重复账号、23 个正常 Token、1 个过期 Token、0 个 inactive Token 泄漏。
- journal 与 release 敏感值扫描：密码/Token 命中数均为 0。
- env 权限：`root:root 0600`；timer：`enabled/active/waiting`，计划在每小时 `:05` 运行。
- 回归：主 API active，crontab 哈希与部署前一致。

## 遗留风险

- 明文 Token 的数据库授权面。
- TikTok API 实际可用性不在本需求验证范围内。

## 发布建议

发布通过。保持 timer 运行并按 journal 的脱敏计数监控；如需紧急止写，执行 `systemctl disable --now socialkit-tiktok-account-sync.timer`，默认保留目标表用于审计。
