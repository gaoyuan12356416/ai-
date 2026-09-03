# 测试报告

## 测试结论

通过。27 项自动化测试、生产 DDL 读回、既有 Token 全量兼容 canary、30 天重建、幂等复跑、最终覆盖、cron 与 DramaWaveMinis `2026-09-02` 样本均已完成。

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
| 数据与调度验收 | 5 | 5 | 0 | 0 |

## 缺陷情况

发现并关闭 `bugs/BUG-001.md`：账户 ID JOIN 的字符集排序规则冲突。修复后生产同日源查询返回 22,921 个 Campaign 候选并完成正式回填。

## 验证证据

- SA 需求评审：`sa-review.md`。
- SA 测试评审：`sa-test-review.md`。
- DDL 评审：`sa-code-review.md`。
- `python -m py_compile`：同步脚本与 Token 轮换脚本通过。
- `python -m unittest discover -s ops/tt-minis-bid-protection -p 'test_*.py'`：27 项全部通过。
- DDL 静态检查：18 列、1 个唯一键、5 个二级索引、ASCII 注释全部通过。
- `git diff --check`：通过。
- 生产 SQL 读回：18 列、1 个业务唯一键、5 个二级索引，与版本库 DDL 一致。
- 精确生产 release：`838b1c6cba12939f6307b1424f51335d67cf9722`；27 项测试通过；上一 release `8668e31373e592b34538fc911d88fa14caa2fa28` 保留。
- Token 全量兼容 canary：3346 的 356 账户、3380 的 68 账户、3416 的 148 账户，共 572 账户；status/history/Native Growth 写入前后均通过。
- SQLite 备份：`/mnt/data-disk/tt-minis-bid-protection/backups/token/tt_business_api_tokens.sqlite3.20260903T041332Z.before_bid_protection`。
- 两次旧写入性能试跑均已安全终止，目标表读回为 0 行；未留下半批数据。第二次试跑证明 `NOW()` 会使 PyMySQL 退化为逐行写入，现已用全参数占位、500 行一提交并增加真批量回归测试。
- 账户级接口探测：缺少 `query_ids` 返回 40002、空数组返回 52404、`ADVERTISER` 层级被拒绝；因此保留 Campaign ID 兼容层。
- 清表前旧数据备份：301,746 行，`/mnt/data-disk/tt-minis-bid-protection/backups/data/20260903T074850Z/ads_tiktok_minis_bid_protection_daily.jsonl.gz`，gzip 校验通过，SHA-256 `a669b43ec1ea2a319d665805e51fe08d71aa02dd6d604808e8f20c4d9f7fa185`。
- 30 天重建：`2026-08-04..2026-09-02`，451,150 个候选，13,199 次账户请求，接口返回并写入 449,022 行，不适用 2,128，失败账户 0，重试积压 0。
- 最终落表：449,022 行、30 个连续日期；仅 `CAMPAIGN`，`adgroup_id` 非空 0，`campaign_id != query_id` 0，金额缩放错误 0，业务重复键 0，越界账户 0，产品 1479 为 0 行。
- 三产品覆盖：DramaWaveMinis 417,662 行/368 个返回账户，BestReelsMinis 6,591 行/127 个返回账户，MyShort 24,769 行/222 个返回账户。源账户范围为 916 个：3346=400、3380=233、3416=283；无消耗或接口不返回历史的账户不强制生成零行。
- 同日幂等复跑：`2026-09-02` 复跑前后总行数均为 449,022、当日均为 22,304，业务重复键仍为 0；复跑跳过 1,568 条终态记录，失败账户 0。
- DramaWaveMinis `2026-09-02`：18,992 条 Campaign 记录，其中 `CONFIRMING=17,641`、`INELIGIBLE=1,298`、`UNDER_PROTECTION=53`；所有币种合计实际赔付 0，无非零 Campaign 明细，失败账户 0。
- root cron 唯一读回 1 行，CPU 时区 `Asia/Shanghai`，`crond` 为 active/enabled；每天 `09:25/21:25` 运行 `--daily`，每次刷新最近 14 个已完成自然日。

## 遗留风险

- 新 Token 已通过当前 572 个可访问账户的全量兼容 canary；未来账户新增或上游权限变化仍需由日任务失败日志暴露。
- TikTok history 接口不支持真正的账户级无 ID 查询：缺少 `query_ids` 会返回 40002。因此业务范围只由账户 SQL 决定，但传输层仍需按账户枚举并分批携带 Campaign ID。
- `2026-09-02` 仍有 17,641 条 `CONFIRMING` 和 53 条 `UNDER_PROTECTION`；后续双日任务会持续回刷最近 14 个完整自然日，最终金额可能随 TikTok 结算更新。

## 发布建议

可以结项。生产 cron 已启用，后续只需按失败日志和重试状态做常规运维，不需要页面、内部查询 API 或飞书通知。
