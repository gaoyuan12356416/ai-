# 定时批次零写入预检恢复

本次只恢复明确指定的 2026-11-02/03 预检失败批次。现有 `recover_failed_preflight_schedule_run` 限同日，且未支持账号服务暂不可用和来源查询失败，因此不伪造时钟调用旧入口，也不扩大它的通用规则。

新入口 `scripts/recover_x_zero_write_schedules.py` 支持 `x_upstream_error`、`x_accounts_unavailable`、`x_post_source_query_failed`。这些错误本身不证明无外部写入，必须同时满足：`failed_preflight`、原始 `started_at` 和 `plan_attempted_at` 为空、零 queue/log、零排队/成功/失败/未知计数。定时发布的持久预约为 queue/pool 绑定；手动素材预约是不同所有者，正常预检和计划事务继续排除其占用。

只读核对显示，账号服务暂不可用批次的错误为共享服务错误，并非所有账户被撤销。原冻结集合中仍有已被 X 暂停的账号，应保留在冻结审计范围，由现有正常预检跳过。当前数据库状态仅是恢复预检资格的证据，不代替 Sidecar 当前授权校验。全体账号均不可用时拒绝恢复；有效子集也仍需正常完整预检。未知外部结果保持原有账号隔离和去重保护。

计划包含原始完整 schedule 行、安全账号状态与身份摘要。apply 先做 SQLite online backup 和 quick_check，再在 `BEGIN IMMEDIATE` 内复核原行和账号状态，写审计并将该行重新置为 `claimed`。仅修改运行状态、错误展示、结束时间及观察时间；原错误留在审计内。原日期、时段、账号顺序、正文、配置版本、随机模板设置、duration policy 和素材快照上界均保留。重复同计划 apply 无害；不创建队列，不访问 X。

`due_schedule_slots` 仅对身份匹配的审计 ID 允许跨 dispatch 切换时间。未审计旧批次保持现状。恢复后的任务仍由正常 runner 校验账号、来源、媒体和唯一性，再创建计划。正在推进的其他批次不被修改或终止；实际执行可以等待当前扫描自然完成。

```bash
python3 scripts/recover_x_zero_write_schedules.py plan \
  --db /var/lib/x-post-automation/accounts.sqlite3 \
  --run-ids <精确逗号分隔ID> --dates 2026-11-02,2026-11-03 \
  --output <私有计划路径>

python3 scripts/recover_x_zero_write_schedules.py apply \
  --db /var/lib/x-post-automation/accounts.sqlite3 \
  --plan <私有计划路径> --plan-sha256 <核对过的摘要> \
  --backup-dir <已验证数据盘上的备份目录> \
  --actor <操作者> --reason <本次用户授权原因> \
  --output <新的私有回执路径>
```

部署需同时包含 `features/pipeline_dispatch/x_schedule_recovery.py`、现有 `x_manual_recovery.py`、修改后的 `features/x_posts/service.py` 和两个恢复脚本（新脚本复用手动恢复的只读连接/校验工具）。使用实际实时计划；如状态或账号发生变化，重新核对并生成计划，不能手改摘要绕过。

本地测试覆盖：完整冻结身份保留、素材上界不变、其他运行中批次不动、未授权旧任务不被领取、重复 apply、三种共享预检错误、未知/计划写入栅栏拒绝、残留 queue 即使汇总为零也拒绝、账号部分不可用及全不可用、计划/账号漂移、备份期间竞态、身份漂移撤销切换例外。线上执行及最终回执以主部署报告为准。
