# SA 代码评审

## 结论

本地静态评审通过；生产 DDL 读回和共享 Token 全量兼容 canary 已通过。首次数据回填及查询验收尚未执行。

## 评审范围

- `ops/tt-minis-bid-protection/001_create_ads_tiktok_minis_bid_protection_daily.sql`
- 同步脚本、单元测试、root cron 配置和 README（完成后纳入）

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | 中 | DDL 唯一键 | 含可空层级字段的唯一键在 MySQL 5.7 不能可靠防重 | 唯一键仅使用非空 `record_date/advertiser_id/data_level/query_id` | 已解决 |
| CR-002 | 中 | DDL ID 列 | 平台 ID 若使用整数可能发生格式/兼容风险 | 广告平台 ID 使用 ASCII binary 字符串；内部 `product_id` 保持数值 | 已解决 |
| CR-003 | 中 | DDL 金额 | 浮点会造成 `/100000` 精度漂移 | 原始值和实际金额均使用 DECIMAL | 已解决 |
| CR-004 | 高 | 同步退出码 | 局部账户失败若退出 0 会把不完整批次误判成功 | 只要存在请求失败即返回退出码 2，同时保留成功 upsert | 已解决 |
| CR-005 | 高 | Token 轮换 | 共享 Token 写入后失败可能影响现有 Native Growth | 写前/写后三产品三接口 canary，SQLite 一致性备份与单行 CAS 回滚 | 已解决 |
| CR-006 | 高 | 产品范围 | `show_name` 预筛会混入或漏掉小程序对象 | 当日全部正消耗对象起步，仅以数值 `product_id` 与 `minis_id` 关系认定 | 已解决 |
| CR-007 | 高 | 历史 API 失败 | 无表内记录的失败对象不会进入待结算回刷 | 数据盘保存脱敏失败候选，后续 `--daily` 自动重试 | 已解决 |
| CR-008 | 高 | Token 权限覆盖 | 每产品单账户 canary 不能证明共享 Token 无缩权 | 冻结旧 Token Native Growth 成功账户全集，新 Token 对全集执行三接口校验 | 已解决 |
| CR-009 | 中 | 并发回滚 | 仅按 Token 哈希回滚可能覆盖并发元数据更新 | 更新与回滚都比较完整预期行，且只恢复本次修改字段 | 已解决 |
| CR-010 | 高 | 生产写入性能 | 旧实现提交粒度过细，首次试跑不能在可接受时间内完成 | 安全终止且确认表为 0 行后，改为批量提交并重新发布 | 已解决 |
| CR-011 | 中 | 终态重复请求 | fresh/retry 范围可能再次包含已落表终态键 | 每日按完整业务键读回终态并在调用 API 前排除，同时清理相同 retry 键 | 已解决 |

## 编译 / 验证结果

- `python -m py_compile ...`：通过。
- `python -m unittest discover -s ops/tt-minis-bid-protection -p 'test_*.py'`：24/24 通过。
- DDL 静态检查：18 列、1 个唯一键、5 个二级索引，注释全部 ASCII。
- `git diff --check`：通过。
- 生产 DDL 读回：18 列、1 个业务唯一键、5 个二级索引，与版本库 DDL 一致。
- Token 全量兼容 canary：3346/356 账户、3380/68 账户、3416/148 账户，共 572 账户；status/history/Native Growth 写前与写后均通过。
- 生产 release：`8668e31373e592b34538fc911d88fa14caa2fa28`；旧 release `2235001` 与 `8ede1c8` 已保留。
