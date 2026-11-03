# 固定短剧批次的一次性调度入口

## 需求、设计与评审边界

仅处理原 run 1029（2026-11-02 16:28 / version 27）和 1031（2026-11-03 08:44 / version 27）。旧调度进程 PID 1478253、InvocationID `329559794abd4c6293cce18757516db5` 从 11:57:10Z 扫描；1036 的长期素材预检使整个 oneshot 尚未返回。入口不停止该进程，不更改并发、FIFO、账号集、正文、素材快照，也不创建新的测试 Post。

并发依据是原始终态及经过审查的旧 runner `43b2aa9d598fba80ef93cde0c7e1ef38794ed78f` 控制流，不能解释成观察到了 Python 线程栈：

- 1029 在该 invocation 内 11:59:14Z 已明确预检失败，记录失败后 worker 返回，没有回到选材或写计划的路径。12:23:47Z 的第三次恢复审计及原失败整行 hash 固定，当前心跳必须仍停在这个恢复时刻。
- 1031 在旧 invocation 开始前 10:52:39Z 已失败，不在旧 due claimed 快照内。要求后续独立 episode 恢复审计发生于旧 invocation 开始之后，且原失败整行 hash 不变。
- 1031 还要求 1029 已有真实 queue 行、plan_attempt 和一致 queued_count。只有汇总计数不能放行。
- 开始执行时旧 PID 和 Invocation 必须仍一致。若已自然结束，应直接使用正常 timer；helper 拒绝新 invocation。helper 执行期间允许旧进程自然结束，避免在正常 plan fence 后仅因无关 scanner 退出而中断。

新增 `schedule-targeted-1029.lock` / `schedule-targeted-1031.lock` 仅绑定这两个原始冻结槽。未来普通 runner 和运维入口共享该互斥；其他身份不触及这些锁。原 `/run/x-post-daily/schedule.lock` 未改动。取得目标锁之后、首次正常查询之前再次核对完整账本和审计，防止计划生成后其他 worker 已推进目标。

入口调用正常 `ScheduleConfig`、`ScheduleSidecarClient`、`execute_schedule_tick`，只返回一个固定 due identity，保持正常账号校验、原素材选择、服务端计划 CAS 和 pipeline handoff。要求 `PIPELINE_X_DISPATCH_ENABLED=1`；包装客户端禁止 legacy `publish_queue`。没有新增选材算法或直接写业务数据库。

## 执行步骤

只部署已 push GitHub 的精确提交。把该提交的 `scripts/x_post_schedule_runner.py` 增量覆盖实际运行树，并核对既有 `drama_selector.py`、`selector.py`、`read_failures.py` 同一提交 blob。无需重启当前 scanner 或 X sidecar。

由 root 暂停 **新** `x-post-schedule.timer` 触发，保留 service PID、claim timer 和发布流程。使用标准 schedule service 的原 Python 和原环境安全加载配置，不输出环境或 token。下列 `PYTHON`、`SOURCE`、`RUNTIME`、`SHA` 和 `EVIDENCE` 均需由操作者填写精确值：

```bash
"$PYTHON" -B "$SOURCE/scripts/run_x_exact_recovered_schedule.py" plan \
  --db /var/lib/x-post-automation/accounts.sqlite3 --run-id 1029 \
  --commit "$SHA" --runner-path "$RUNTIME/scripts/x_post_schedule_runner.py" \
  --repository "$SOURCE" --output "$EVIDENCE/1029-plan.json"
"$PYTHON" -B "$SOURCE/scripts/run_x_exact_recovered_schedule.py" run \
  --plan "$EVIDENCE/1029-plan.json" --actor operator \
  --output "$EVIDENCE/1029-result.json"
```

每次仅一个 run。1031 必须先由既有 `recover_x_drama_episode_selection.py` 精确审计恢复，再以 `--run-id 1031` 生成新 plan/run；不可把 1029 的计划替换 ID 使用。1029 尚未真实建队列或 1031 尚未恢复时拒绝。

另有全局运维互斥 `schedule-targeted-operator.lock`，防止两个入口同时运行。每个 run+恢复审计 hash 在 `/mnt/data-disk/x-publish-recovery-20261103/targeted-planner` 写入不可覆盖的一次性调用证据，结果写单独 `.result.json`。发生超时、未知、失败或进程中断均不删除 marker 重跑；先查账本并走正常恢复审计。正常 runner 的未知计划 fence 保持有效。

完成后核对真实队列/结果和原 scanner PID，再由 root 恢复 timer。若旧 scanner 在开始前已结束，跳过此入口，直接恢复普通 timer。每批 35 个账号的正常只读预检通常需数分钟；网络重试和素材查询可能延长，不能承诺固定完成时间。helper 不等待上传完成。

## 测试与风险

专项覆盖：两个固定身份；锁忙不读目标/账号；完整恢复审计链及 hash 漂移；心跳、快照、unknown、plan fence、真实队列拒绝；锁内二次核验；1031 顺序依赖；一次性 marker；开启 pipeline；运行树漂移；旧 PID/Invocation/timer；旧进程自然结束；正常 tick 唯一 create-plan 和未知响应不重发；从不调用 legacy Post。

运行命令：`python -m unittest scripts.test_x_exact_recovered_schedule_runner scripts.test_x_post_schedule_runner scripts.test_x_schedule_read_preflight_recovery scripts.test_x_zero_write_schedule_recovery scripts.test_x_drama_episode_selection_recovery scripts.test_x_schedule_scope_churn_recovery scripts.test_x_read_preflight_retry -q`：134 项通过；三个相关 Python 文件的 `py_compile` 与 `git diff --check` 通过。生产执行未包含在本地测试中。旧 invocation 的固定审计证据不适用于任意其他进程或未来失败；禁止改 ID/anchor 扩用。
