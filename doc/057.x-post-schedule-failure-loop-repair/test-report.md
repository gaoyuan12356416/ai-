# 测试报告

## 测试结论

本地离线测试通过，可以进入 GitHub-first 备份与部署。测试过程没有调用真实 X 发布接口。

## 测试范围

Selector 安全查询、素材 FIFO/语言容量、计划 known/unknown outcome、计划写围栏、跨午夜租约、Run 274 精确恢复、OAuth sidecar、daily/manual/catchup/relay/drama 既有路径及 SQLite 迁移。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 聚焦变更回归 | 181 | 181 | 0 | 0 |
| 完整 X 回归 | 796 | 796 | 0 | 0 |
| 预期跳过 | 2 | 2 | 0 | 0 |

## 缺陷情况

- BUG-001 至 BUG-004 均已修复并回归。
- 独立代码审查发现 schedule 查询 DTO 丢失 `plan_attempted_at`、容量证据可由更旧候选事后凑满、历史 available 错误导致 proof=0 循环三个 P1；均已修复并新增负例。
- 首次全回归发现 5 个新增稳定错误码未进入中文错误目录；补齐后目录测试与完整回归通过。
- 首次全回归的 2 个 Windows 本地 HTTP 连接中止用例独立复跑 2/2 通过，完整复跑未重现。
- 生产 validate-only 门禁首次发现恢复工具漏接两个已在线使用的非敏感 schedule 键；该次零 DB/X 写入，补 allowlist 后 9/9 聚焦及 796/796 全回归通过。

## 验证证据

- `python -m py_compile ...`：通过。
- `python -m unittest scripts.test_x_post_material_pool_selector scripts.test_x_post_schedule_runner scripts.test_x_post_multi_schedule_store scripts.test_x_post_bound_drama_media_recovery`：181/181 通过。
- `python -m unittest discover -s scripts -p "test_x_*.py"`：796/796 通过，2 项预期跳过。
- `git diff --check`：通过。

## 遗留风险

- 生产 Run 274 必须在部署提交上重新做 14 条真实媒体 validate-only；结果通过后才允许原子 apply。
- 实际恢复发布只允许由自然 timer 消费 frozen queues；其外部发布结果需以 ledger 和 X readback 为准。

## 发布建议

同意在完成独立代码审查、GitHub 推送、SQLite online backup 和备份副本迁移验证后部署；任一生产清单漂移即停止，不做部分恢复。
