# 测试报告

## 测试结论

修订版本地阶段通过，生产数据验收仍在进行中。27 项自动化测试、生产 DDL 读回、既有 Token 全量兼容 canary，以及账户级接口约束探测已通过；30 天重建、幂等、最终覆盖、cron 与样本核对尚未执行，因此当前不作完整发布通过声明。

## 测试范围

- MySQL 5.7 单表 DDL 与索引。
- 精确账户范围、Campaign 单层接口同步、金额缩放和幂等。
- Token 安全轮换、局部失败、日志脱敏和每日调度。
- 30 天回填、每天两次 14 天刷新与 DramaWaveMinis `2026-09-02` 样本。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 文档/设计评审 | 2 | 2 | 0 | 0 |
| DDL 静态评审 | 3 | 3 | 0 | 0 |
| 自动化测试 | 27 | 27 | 0 | 0 |
| 生产结构验收 | 1 | 1 | 0 | 0 |
| Token 全量兼容验收 | 1 | 1 | 0 | 0 |
| 数据与调度验收 | 5 | 0 | 0 | 5 |

## 缺陷情况

当前无确认缺陷，因此未保留 BUG 占位文件。

## 验证证据

- SA 需求评审：`sa-review.md`。
- SA 测试评审：`sa-test-review.md`。
- DDL 评审：`sa-code-review.md`。
- `python -m py_compile`：同步脚本与 Token 轮换脚本通过。
- `python -m unittest discover -s ops/tt-minis-bid-protection -p 'test_*.py'`：27 项全部通过。
- DDL 静态检查：18 列、1 个唯一键、5 个二级索引、ASCII 注释全部通过。
- `git diff --check`：通过。
- 生产 SQL 读回：18 列、1 个业务唯一键、5 个二级索引，与版本库 DDL 一致。
- 精确 release：`8668e31373e592b34538fc911d88fa14caa2fa28`；24 项测试通过；旧 release `2235001` 与 `8ede1c8` 保留。
- Token 全量兼容 canary：3346 的 356 账户、3380 的 68 账户、3416 的 148 账户，共 572 账户；status/history/Native Growth 写入前后均通过。
- SQLite 备份：`/mnt/data-disk/tt-minis-bid-protection/backups/token/tt_business_api_tokens.sqlite3.20260903T041332Z.before_bid_protection`。
- 两次旧写入性能试跑均已安全终止，目标表读回为 0 行；未留下半批数据。第二次试跑证明 `NOW()` 会使 PyMySQL 退化为逐行写入，现已用全参数占位、500 行一提交并增加真批量回归测试。
- 账户级接口探测：缺少 `query_ids` 返回 40002、空数组返回 52404、`ADVERTISER` 层级被拒绝；因此保留 Campaign ID 兼容层。
- 30 天重建、幂等、最终覆盖、root cron 状态和样本结果待实际执行后补充。

## 遗留风险

- 新 Token 已通过当前 572 个可访问账户的全量兼容 canary；未来账户新增或上游权限变化仍需由日任务失败日志暴露。
- 尚未执行旧事实备份/清空、30 天回填、同范围幂等复跑、最终落表覆盖、cron 安装/自然触发及 `2026-09-02` 样本查询。

## 发布建议

暂不建议任务结项或启用 cron；须完成 30 天重建、幂等、三产品账户/Campaign 单层覆盖和 `2026-09-02` 样本核对后更新为通过。
