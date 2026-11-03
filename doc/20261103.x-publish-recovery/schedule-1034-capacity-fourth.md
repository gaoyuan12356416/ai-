# 1034 第四次精确恢复：计划事务前容量拒绝

## 范围与设计审查

只允许原 material 批次 1034（2026-11-03 08:45、配置 33、89 个原账号、随机视频模板、素材池快照 4444）。2026-11-03T13:05:17Z 的原错误为 `x_post_pool_fifo_conflict` / `素材语言尚未达到冻结批次容量`；`plan_attempted_at` 已被 runner 记录，不能套用普通“未尝试 plan”恢复。

已核对当时 X native 179596a 的 `create_schedule_plan`：该具体 HTTP 409 在首个 `BEGIN IMMEDIATE` 前抛出。工具从固定历史 Git commit 验证函数原文 SHA 与错误位置，绑定原完整失败行及错误字段 SHA；不是凭 `queue_count=0` 推断任意写入结果。

2026-11-03T13:35:44Z 的只读核验确认 1034 仍无队列；1036 已在 13:31:33 建成 42 条队列 5667–5708。工具要求这些原队列身份仍在，允许其发布状态自然前进，不等待发布完成。

## 实现与审计

入口为 `scripts/recover_x_schedule_1034_capacity.py`。没有任意 run-id 参数。固定绑定前三张审计表的原行 SHA：首次 zero-write、scope-churn、read-preflight。新表 `x_post_schedule_1034_capacity_resume_audit` 单独保存原失败行、原 `plan_attempted_at`、三份旧审计、历史错误证明、已部署规划代码证明、1036 队列身份、操作人、原因和备份 SHA，拒绝更新、删除及 replace。

仅在备份完成后、同一个 `BEGIN IMMEDIATE` 内重新检查全量前提，新增第四审计并 CAS：1034 回到 `claimed`，清当前错误/完成时间/计划尝试 fence，续 heartbeat。原尝试时间完整保留在审计专列及原始行中；原身份、89 账号、快照、计数和前三审计不变。重复 apply 对后续已经进行的工作只返回 no-op，不重新 rearm。任何其它错误、失败时间、队列、未知计数、账号/快照漂移、审计变动或另一维护者的状态变化均拒绝。

normal runner 继续原任务；工具不选材、不创建队列、不发帖、不调用网络、不修改 1036，也不改 material/drama 并发方式。事务内只做本地文件 SHA 校验，不运行 Git 子进程或远程请求。

## 部署与执行

先提交到 GitHub，再由主维护者拉取已核验 commit。此工具无需部署或重启共享 runtime。`--repair-commit` 应为实际已部署 runner/selector/read-retry 文件对应的完整 commit；当前为 19504a5 所属完整 SHA。已部署文件仍须逐项与 Git blob 匹配。

```bash
python scripts/recover_x_schedule_1034_capacity.py plan \
  --db /var/lib/x-post-automation/accounts.sqlite3 \
  --repair-commit <deployed-full-sha> --runner-path <deployed-scripts/x_post_schedule_runner.py> \
  --repository /mnt/data-disk/x-publish-recovery-20261103/source \
  --output <private-maintenance-dir>/1034-fourth-plan.json

python scripts/recover_x_schedule_1034_capacity.py apply \
  --db /var/lib/x-post-automation/accounts.sqlite3 \
  --plan <private-maintenance-dir>/1034-fourth-plan.json \
  --backup-dir <private-maintenance-dir>/backup \
  --actor <operator> --reason 'Exact pre-transaction capacity rejection; original identity and prior audits retained' \
  --output <private-maintenance-dir>/1034-fourth-result.json
```

执行后核对审计中的旧尝试时间、当前原身份/快照及 `claimed`，交普通 runner；若已被领取则只观察其回执。不得恢复整个 SQLite 备份覆盖正在推进的其它批次，不得重复 rearm 后续新失败。

## 测试与交付边界

专项 `python -m unittest scripts.test_x_schedule_1034_capacity_recovery`：10 项通过。覆盖真实 SQLite 一次恢复、旧尝试/三审计保留、普通授权入口可见、重复执行不改已推进状态、未来同错误/未知/漂移拒绝、真实队列反证、1036 前置计划与自然进展、备份后 CAS 漂移、部署和历史源码漂移、审计不可覆盖。

联合 read-preflight、scope-churn、首次 zero-write 和随机视频规划回归共 42 项通过，`py_compile`、`git diff --check` 通过。另将 13:35:44 的真实只读快照载入本地内存 SQLite，未 mock 固定锚点的 `snapshot` 校验通过；真实 Git 179 函数 SHA 及第 12871 行拒绝早于第 12885 行事务开始的校验通过。

此交付只提交代码与测试，不执行生产 apply。容量拒绝的原因有账号发布完成造成可用范围变化的证据；没有据此宣称 material 串行能够根治，也未实施串行调度。
