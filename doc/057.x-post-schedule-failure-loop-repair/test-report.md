# 测试报告

## 测试结论

代码与生产门禁均通过并已部署。离线测试没有调用真实 X 发布接口；生产验收只恢复原 frozen queues，由自然 timer 消费，没有制造测试 Post。

## 测试范围

Selector 安全查询、素材 FIFO/语言容量、计划 known/unknown outcome、计划写围栏、跨午夜租约、Run 274 精确恢复、OAuth sidecar、daily/manual/catchup/relay/drama 既有路径及 SQLite 迁移。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 首版聚焦变更回归 | 181 | 181 | 0 | 0 |
| BUG-005 聚焦回归 | 193 | 193 | 0 | 0 |
| 最终 Linux 完整 X 回归 | 800 | 800 | 0 | 0 |
| 预期跳过 | 2 | 2 | 0 | 0 |

## 缺陷情况

- BUG-001 至 BUG-005 均已修复并完成聚焦回归。
- 独立代码审查发现 schedule 查询 DTO 丢失 `plan_attempted_at`、容量证据可由更旧候选事后凑满、历史 available 错误导致 proof=0 循环三个 P1；均已修复并新增负例。
- 首次全回归发现 5 个新增稳定错误码未进入中文错误目录；补齐后目录测试与完整回归通过。
- 首次全回归的 2 个 Windows 本地 HTTP 连接中止用例独立复跑 2/2 通过，完整复跑未重现。
- 生产 validate-only 门禁首次发现恢复工具漏接两个已在线使用的非敏感 schedule 键；该次零 DB/X 写入，补 allowlist 后 9/9 聚焦及 796/796 全回归通过。
- 生产 Run 274 的 14/14 validate-only 通过；首次 apply 暴露 relay 141 秒 hint 与修复后真实 `<=140` 的触发器冲突。事务完整回滚、零 X 写入。补丁保留 frozen relay，只开放 immutable audit 完全匹配的一次性例外。
- BUG-005 初版后本地 798 项完整回归连续三次各只有 1 个随机 Windows loopback `WinError 10053`，失败端点每次不同且隔离复跑通过；业务断言零失败。独立复核随后将 relay 例外从任意旧值 `>140` 收紧为精确历史占位值 `141`，增加 `142/180` 整事务回滚负测，并把 6 个剧集冻结身份字段纳入防篡改触发器；Linux 800/800 是后续发布硬门禁，不把本机 socket 抖动计为通过。
- 最终独立复核 P0/P1=0；普通 `<=140` relay INSERT/UPDATE 仍拒绝，精确 141 恢复、142/180 回滚、frozen drama identity 防篡改、CLI SQLite 脱敏均通过。
- 自然发布证明恢复后的短/长 relay 不再出现媒体维度或 trigger 错误。唯一失败是 X 对目标账号 8 明确返回 HTTP 403 暂时锁定；source Post 已成功，repost 未成功，attempt=1、unknown=0，系统未自动重试。

## 验证证据

- `python -m py_compile ...`：通过。
- `python -m unittest scripts.test_x_post_material_pool_selector scripts.test_x_post_schedule_runner scripts.test_x_post_multi_schedule_store scripts.test_x_post_bound_drama_media_recovery`：181/181 通过。
- `python -m unittest scripts.test_x_post_multi_schedule_store scripts.test_x_post_bound_drama_media_recovery scripts.test_x_post_premium_relay_repost scripts.test_x_post_schedule_runner`：193/193 通过。
- Linux `python3 -m unittest discover -s scripts -p "test_x_*.py"`：800/800 通过。
- `git diff --check`：通过。
- 备份副本 apply、live validate-only、live apply：均 14/14；queue 总数/Run 345/绑定不漂移，quick=ok、FK=0、`x_write_attempted=false`。
- 自然验收：Run 274 最终 published=15、failed=1、unknown=0；恢复的 14 条为 13 成功 + 1 X 账号锁定终态失败。

## 遗留风险 / 当前阻塞

- X 账号 8 `NaughtyLovm57c` 需要人工登录 X 完成解锁。解锁前 schedule/claim timers 保持 inactive；manual timer 与 sidecar 不受影响。
- queue 533 已有成功的 relay source Post，禁止从头重发。后续只能在身份/readback 确认后执行一次审计化 repost-only 恢复。
- Run 345 停在 claimed、`plan_attempted_at=''`、queue=0 的安全点；账号解锁后可继续验证 BUG-001 的 live 计划路径。

## 发布建议

代码发布与 Run 274 恢复验收完成。账号 8 解锁并完成 repost-only 补偿前，不恢复 schedule/claim timers；严禁盲目重跑 queue 533 或整库回滚。
