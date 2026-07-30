# SA 测试用例评审

## 结论

通过，关闭态版本已完成测试并上线。自动化测试 275/275 全部通过：TT 154/154、X 93/93、素材状态 28/28。即使全部通过也不代表可开启真实 Direct Post。

2026-07-29 最终评审：TC-030–TC-047 及既有 TT/X/素材状态回归均已通过自动化验证；TC-048 的登录态浏览器验收已完成。CPU 已切换至 `/opt/tt-post/releases/5cfc657`，GPU release 未变。三项 Direct Post 门禁均为 0，验收未创建任务。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | TC-003 | 仅测成功 Token 会误判账号可发 | 加入 scope、过期、禁发和限额失败 | 已补充 |
| STR-002 | TC-017 | 普通重试测试不能覆盖重复发帖风险 | 单独验证 unknown 永不自动 init | 已补充 |
| STR-003 | TC-018 | 需证明门禁未发外部请求 | 对 init client 做调用次数断言 | 已补充 |
| STR-004 | TC-023 | Token 泄漏需跨介质检查 | 覆盖 API、异常、日志、SQLite、manifest | 已补充 |
| STR-005 | TC-026 | 线上是整合分支 | 加 X 和主后台既有测试回归 | 已补充 |
| STR-006 | TC-033/034 | 只测“整批失败”无法证明页面逐项隔离 | 分开覆盖 preview 中间失败和 queue 中间失败，并断言后续项仍执行 | 已补充 |
| STR-007 | TC-035–039 | 单一时间字段与账号时间唯一约束可能冲突 | 覆盖默认/自定义间隔、边界、preview 失败不占槽、queue 失败不前移和历史时点冲突 | 已补充 |
| STR-008 | TC-040–042 | 页面字符计数不能替代服务端安全校验 | 增加缺失/未知占位符、正确别名、UTF-16 2200/2201 和模板/文案不一致 | 已补充 |
| STR-009 | TC-043/044 | 单条重试不足以覆盖批量响应丢失和改描述重试 | 精确重放全部子请求并断言原 ID；同 key 改模板须在 Service/Core 双层冲突 | 已补充 |
| STR-010 | TC-045 | 新默认模板可能破坏历史省略字段的重放 | 覆盖历史固定、自定义 `caption_text` 和缺省默认模板三类任务 | 已补充 |
| STR-011 | TC-046 | 仅比较输出 URL 不能证明避免重复制作 | 比较确定性 job 身份与 GPU prepare 调用；同时验证源/profile 变化会失效 | 已补充 |
| STR-012 | TC-047 | 新增批量端点/表会扩大部署和回滚面 | 合同测试固定现有路由白名单及四表 schema，确认不新增批次表 | 已补充 |
| STR-013 | TC-048 | 100 项顺序请求可能长时间无反馈或错误汇总 | 浏览器验证实时进度、完成统计、失败明细和凭据不泄漏 | 已补充 |

## QA 修订确认

2026-07-29：初版测试矩阵已纳入合规、幂等、Token 和双服务回归。

2026-07-29：增量矩阵已纳入批量解析、逐项部分失败、时间序列、可编辑模板、UTF-16、prepare 复用、多任务幂等和历史兼容，相关自动化均通过。

2026-07-29：Chrome 登录态验收确认批量输入框、20 位 ID 前端拦截、当前默认模板可见且可编辑、默认间隔 10 分钟、账号设置只读消费和未配置时建队禁用；全过程未创建队列任务。

2026-07-29：确认既有 TT 个号设置原子批量保存能力未因本轮发布池改动发生回归。

## 2026-07-30 增量测试评审（仅本地）

### 结论

TC-057–TC-079 已补齐本轮增量自动化覆盖。本轮本地自动化 TT 205/205（Core 49、Service + Runner 77、GPU 33、发布池 UI 23、个号设置 UI 11、App contract 12），X 351/351（skipped 1），素材状态 28/28，总计 584/584。该结果证明仓库内状态机、接口、页面合同及既有回归通过，但不证明生产部署、2.36GB COS 上传重跑、线上定时执行或真实 TikTok 发布已经通过。

### 用例与自动化证据

| 用例 | 主要自动化证据 | 评审结果 |
| --- | --- | --- |
| TC-057 | `test_resolver_accepts_long_tt_video_and_keeps_shared_safety_checks`；X `test_pool_order_is_created_at_then_id_and_does_not_use_insight` 固定断言 `1,140` | 通过 |
| TC-058 | `test_schedule_defaults_disabled_and_saves_with_optimistic_version`、`test_daily_schedule_time_is_unique_and_released_on_change_or_disable`、`test_daily_schedule_time_conflict_is_atomic_across_store_instances`、`test_pool_fifo_is_isolated_per_account`、`test_daily_due_is_slot_idempotent_and_fifo` | 通过 |
| TC-059 | `test_runner_rejects_any_grace_other_than_ten_minutes`、`test_daily_slot_retries_within_grace_after_manual_account_lock`、`test_overdue_schedule_is_marked_missed_and_never_claimed` | 通过 |
| TC-060 | `test_claimed_unbound_runs_can_be_found_by_key_and_recovered_in_order`、`test_daily_runner_recovers_claim_before_freeze_across_minutes` | 通过 |
| TC-061 | `test_bind_and_sync_follow_legacy_queue_without_changing_its_machine`、`test_manual_retry_recovers_queue_committed_before_run_binding` | 通过 |
| TC-062 | `test_manual_publish_reuses_pending_key_until_server_success`、`test_manual_publish_response_status_controls_operator_message_and_key`、`test_pending_manual_keys_are_session_scoped_validated_and_per_account` | 通过 |
| TC-063 | `test_runner_schedules_first_claims_singly_and_never_republishes_unknown`、`test_publish_business_error_defers_later_claim_to_next_tick`、`test_new_daily_queue_uses_remaining_claim_budget_and_safe_result` 验证 `deferred_count`/`oldest_deferred_at_utc` 透出、`test_reconcile_backlog_starts_only_after_due_claim_publish`、`test_reconcile_response_cannot_exceed_per_tick_budget` 验证超量 fail-closed | 通过 |
| TC-064 | `test_live_gates_default_closed_and_require_all_three`、`test_closed_gates_manual_publish_does_not_consume_material`、GPU closed-gate 测试 | 通过 |
| TC-065 | `test_execution_lease_is_per_run_exclusive_and_crash_recoverable`、`test_release_first_fences_stale_queue_freeze`、`test_queue_freeze_first_blocks_release_until_owner_binds`、`test_claim_without_queue_releases_only_after_600_seconds`、`test_expired_owner_cannot_freeze_after_new_owner_preflight_release` | 通过 |
| TC-066 | `test_unconfigured_or_loading_account_cannot_inherit_prior_account_time` | 通过 |
| TC-067 | `test_legacy_exact_queue_creation_is_not_publicly_writable`、`test_queue_actions_use_dynamic_sidecar_routes_and_safe_audit` | 通过 |
| TC-068 | `test_default_prepared_output_ceiling_matches_tiktok_four_gib`、部署样例 `TT_POST_GPU_MAX_OUTPUT_BYTES=4294967296` 断言 | 通过 |
| TC-069 | 部署合同断言 sidecar 持有 `RuntimeDirectory=tt-post` 且 oneshot runner 不声明同名目录 | 通过 |
| TC-070 | `test_ready_manifest_is_revalidated_against_current_output_limit` 用 subtests 篡改 content/job/SHA/精确 URL/probe/profile；另从 publish 验证 `prepared_media_invalid` 发生在 TikTok init 前 | 通过 |
| TC-071 | GPU `test_configuration_defaults_to_exact_closed_gates_and_loopback`、`test_deploy_units_are_sandboxed_and_gate_names_have_no_aliases` 固定内部 8700；Service `DeployContractTests` 与 App contract 固定外层 CPU prepare 9000、主应用 exact preview 9060、nginx exact preview 9120，以及 runner 60/1500/2400/1500、systemd 5700 和关闭门禁 | 通过 |
| TC-072 | `test_publish_renews_claim_across_slow_credentials_and_gpu_calls` 验证每次读取凭据后续租，第一段覆盖 Creator Info，第二段跨过 Creator Info 并衔接 TikTok init/GPU publish；续租时长固定为 GPU 普通超时 + 60 秒 | 通过 |
| TC-073 | `test_manual_crash_retry_uses_original_frozen_minute`、`test_expired_owner_cannot_freeze_after_new_owner_preflight_release` 验证 recurring 慢 Creator 预检不持有 120 秒 execution lease；预检成功后原子取得当前 fencing lease，失败释放前重新取得并核验当前 owner，旧 owner 无法 freeze 或释放新 owner 的素材 | 通过 |
| TC-074 | `test_daily_schedule_time_is_unique_and_released_on_change_or_disable`、`test_daily_schedule_time_conflict_is_atomic_across_store_instances` 验证启用账号的 Asia/Shanghai `HH:MM` 全局唯一、保存事务原子冲突、改时或停用释放时点；发布池 UI 合同验证“不同账号需选择不同的分钟” | 通过 |
| TC-075 | `test_cos_upload_request_timeout_disables_sdk_retries` 验证 `CosConfig.Timeout=120`、`KeepAlive=false` 与 SDK `retry=0` | 通过 |
| TC-076 | `test_cos_upload_uses_bounded_manual_multipart_and_verifies_object` 验证 8MiB 分片、MD5/ETag/complete 及最终 HEAD size/SHA | 通过 |
| TC-077 | `test_cos_part_deadline_does_not_wait_for_executor_shutdown` 验证 future 超时路径不执行 executor wait 并异步 abort；`test_prepare_total_deadline_is_shared_across_pipeline_stages` 验证全流程共享 8700 秒内部预算 | 本地通过，生产待验证 |
| TC-078 | `test_cos_complete_timeout_is_not_aborted_and_retry_recovers_head` 验证 complete outcome unknown 不 abort，稍后完成后同内容重试经 HEAD 恢复且不创建第二次 multipart | 通过 |
| TC-079 | `test_cos_part_concurrency_is_bounded_across_store_instances` 以两个 Store、8 个并发 part 验证进程级共享 4 槽 semaphore，峰值不超过 4 且结束后槽位释放 | 通过 |

### 覆盖性复核

- 4665764 场景使用其 2087 秒属性的本地 fixture；自动化未访问生产素材库，因此生产中的当前 URL、时长和映射仍须单独验收。
- TT 3600 与 X 140 分别由不同模块测试固定，避免“TT 修复通过但 X 合同被静默放宽”。
- 两个崩溃窗口均采用故障注入，不只检查最终成功，还检查中间 run/queue/pool 数量和身份。
- 手动幂等覆盖同账号重试、页面刷新、多账号隔离、响应状态分类、非法 session 数据清洗和无 `innerHTML`/凭据暴露。
- Runner 每个 tick 固定 `schedules_due(limit=1)`、`claim(limit=1)`、`reconciling/reconcile limit=1`，先处理到期 schedule，再单条 claim/publish，最后单条 reconcile；service 返回、runner 日志透出 `deferred_count` 与 `oldest_deferred_at_utc`；reconcile 多返回时 fail-closed；最坏远端等待仍为 5520 秒，5700 秒 systemd 上限保留 180 秒本地开销余量。
- 门禁用例断言 pool 不被消费，而非只断言远端 publish 没有调用。
- 并发竞态覆盖 120 秒 per-run lease、token 轮换、release-first、freeze-first 和旧 owner 失权；recurring 慢 Creator 预检不占用该 lease，成功后才原子取得当前 fencing lease，失败释放前必须重新取得且不能越过其他 live owner。
- 账号切换用例固定检查未配置/加载态恢复 `11:00`，避免跨账号 UI 状态泄漏。
- 主应用合同用例固定精确 `/queue` 为 GET-only，同时检查动态 cancel/reconcile 路由仍存在且不暴露 claim token/caption。
- ready manifest 不是永久可信缓存；同一测试除“先按宽合同生成、再收紧配置”外，还以 subtests 实际篡改 content、job、SHA、精确 COS URL、probe 和 profile，prepare 全部 fail-close，并证明 publish 在 TikTok init 前拒绝篡改 manifest。
- 超时按操作和路由分离：COS 单请求 120 且 SDK retry=0；prepare 全流程共享 8700 秒内部 deadline 预算；外层 CPU prepare 9000、主应用 exact preview 9060、nginx exact preview 9120；GPU 普通 900；runner 通用 60/schedule 1500/publish 2400/reconcile 1500；systemd runner 5700。内部到 CPU 的 300 秒余量覆盖单次读/清理，不声称严格在第 8700 秒返回。
- COS 使用每片 8MiB、每批最多 4 片的有界手工 multipart；模块级共享 4 槽 semaphore 跨 Store 约束进程内 part 并发。future 超时路径不等待 executor，multipart 由 daemon 线程异步 abort；complete 已开始且结果未知时例外，不 abort，后续经 HEAD 恢复。
- queue 初始 claim lease 仍为 300 秒且小于 600 秒 grace；`publish_claimed` 每次读取凭据后续租到 GPU 普通超时 + 60 秒，第一段覆盖 Creator Info，第二段衔接 TikTok init/GPU publish，与单条 claim 共同避免慢凭据、慢预检和后续任务提前持有 lease。
- 所有启用账号的 Asia/Shanghai `HH:MM` 在保存的 `BEGIN IMMEDIATE` 事务中全局唯一；冲突返回 `tt_post_schedule_time_conflict` / HTTP 409，页面明确提示“不同账号需选择不同的分钟”，改时或停用会释放旧时点。
- 生产关闭态已确认约 5100 秒后本地 2.36GB 成片完成，但 manifest 仍为 1、job 为 0，COS 近期只有旧约 45MB 对象；失败根因仍为推断，须按正式 COS/deadline 合同重跑验证。

### 生产测试待填写

| 验收项 | 结果 |
| --- | --- |
| 生产 release/commit、备份和回滚 | 待填写 |
| 七表迁移与 SQLite integrity | 待填写 |
| 4665764 真实 preview、GPU 成片与目标账号时长 | 待填写 |
| 正式 COS 120 秒/零重试/4×8MiB/共享 4 槽 semaphore/complete unknown 不 abort/prepare 8700 秒内部预算合同下的 2.36GB 新对象、ready manifest/job 与失败根因 | 待重跑验证 |
| timer/path、600 秒宽限、schedule/claim/reconcile 各限 1 条、积压日志、reconcile 超量 fail-closed、5520 秒远端等待上界与 5700 秒总预算 | 待填写 |
| 慢凭据续租、recurring 慢 Creator fencing 接管与旧 owner 拒绝 | 待填写 |
| 启用账号 Asia/Shanghai `HH:MM` 全局唯一、409 冲突与改时/停用释放 | 待填写 |
| 门禁关闭时 pool/run/queue 不消费 | 待填写 |
| 登录态每日排期、FIFO、跨刷新和多账号手动幂等 | 待填写 |
| 公网 no-store、静态 hash 和外部请求计数 | 待填写 |

生产表格未填写前，测试评审结论保持“本地通过、生产待验收”。
