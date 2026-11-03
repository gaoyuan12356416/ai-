# 零写计划的只读预检故障

1029 在 2026-11-03T11:59:14Z 因 `x_post_drama_query_failed / read-only drama query failed: OperationalError` 失败；1034 在 11:57:50Z 因 `x_upstream_error / X API网络请求失败` 失败。二者均未登记计划写入、无队列、结果计数为零。旧代码丢弃 MySQL errno，且 X 账号 HTTP 客户端把网络错误与非 JSON 合为同一信息，因此不将历史信息泛化为瞬时错误许可。

现场只读复核：使用原 planner 配置对 1029 原账号绑定的 pool 2 执行完整剧集审计成功（11 集）；1034 原账号范围内当前可用账号 16、14 的正常 schedule_preflight verify 成功、active/publish_eligible。未调用 plan 或 Post。当前检查证明读路径可用，不证明历史具体 errno。

新逻辑仅在明确只读边界最多调用两次：账号 verify 的结构化 sidecar 网络错误或真实 5xx，及只读 SELECT/connect 的连接错误。MySQL 只接受已知驱动 OperationalError 的 2002/2003/2006/2013/2055；权限、SQL、配置错误和未知 errno 不自动重试。源查询失败仅保留安全类型/errno，不输出 SQL、地址、请求或 token。剧集查询重连一次，继续同一个 pool scope。旧泛化的 `X API网络请求失败` 不进入自动等待；明确 HTTP 4xx、未知写入、非 JSON、一般异常及 pool/audit 修改错误也不进入等待。

有限读取重试耗尽后，重新查询原批次及续租回执，两次都必须证明原身份/快照、claimed、plan_attempted_at 和 started_at 为空、全部结果计数零、无队列，才返回 waiting_preflight 并等待下一 tick。drama 的原空 material snapshot 保持空；material 已冻结上界保持不变。任何漂移或证据读取失败只停止当前 tick，不清除原账本。已跨 plan 写入栅栏或已有队列的路径完全不复用这个分支。

只需增量部署同一 Git commit 的四个文件，不重启当前 planner 或 sidecar：

- scripts/x_post_schedule_runner.py
- features/x_posts/drama_selector.py
- features/x_posts/selector.py
- features/x_posts/read_failures.py

`recover_x_schedule_read_preflight.py` 是独立精确恢复工具，限当前 1029/1034 两次已知零写失败。完整原 run SHA、失败时间和冻结身份硬锚定；1029 引用首次审计，1034 同时引用首次和第二次审计，旧表不修改。新表 `x_post_schedule_read_preflight_resume_audit` 保存原错误、全部前审计、备份 SHA 和部署证据。备份后重新在事务内核验再 CAS claimed，重复同计划不重置后续进展。1039 已有 38 队列，明确排除。

```bash
python -B scripts/recover_x_schedule_read_preflight.py plan --db /var/lib/x-post-automation/accounts.sqlite3 \
  --run-ids 1029,1034 --repair-commit FULL_COMMIT --runner-path DEPLOYED_RUNNER --repository GIT_SOURCE --output PLAN.json
python -B scripts/recover_x_schedule_read_preflight.py apply --db /var/lib/x-post-automation/accounts.sqlite3 \
  --plan PLAN.json --backup-dir PRIVATE_BACKUPS --actor OPERATOR --reason EXACT_ORIGINAL_ZERO_WRITE_RECOVERY --output RESULT.json
```

plan/apply 都逐文件核对实际部署字节与指定完整 Git commit 一致。本工具不发送平台请求、不改并发、不重选任何已有队列、不把原尝试或审计清零。
