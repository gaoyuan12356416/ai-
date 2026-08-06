# 测试报告

## 状态

自动化测试、旧 TT 回归、真实浏览器无发布验收和最终安全复核已通过；生产关闭默认验收须在后续明确授权部署后执行。

## 结果

| 范围 | 命令/方法 | 结果 |
| --- | --- | --- |
| 新系统单元、契约与 UI 测试 | `python -m unittest scripts.test_tt_auto_post_store scripts.test_tt_auto_post_selector scripts.test_tt_auto_post_metrics scripts.test_tt_auto_publish_ui scripts.test_tt_auto_post_service scripts.test_tt_auto_post_publisher scripts.test_tt_auto_post_links scripts.test_tt_auto_code_broker scripts.test_tt_auto_publish_app_contract scripts.test_tt_auto_post_runner -v` | 117/117 通过 |
| 旧 TT 回归 | `python -m unittest scripts.test_tt_post_pool_ui scripts.test_tt_account_settings_ui scripts.test_tt_posts_app_contract -v` | 64/64 通过 |
| 生产 MySQL 5.7 兼容 | 在 `ONLY_FULL_GROUP_BY` 开启的只读生产连接上执行指标 SQL `EXPLAIN` | 通过；使用 `pss` 索引，无 errno 1055 |
| 旧系统文件边界 | `git diff --exit-code -- features/tt_posts static/tt-post-pool.html static/tt-account-settings.html` | 通过，无差异 |
| 新前端语法 | 四个新 JS 与 `quick-nav.js` 执行 `node --check` | 通过 |
| 浏览器无发布验收 | Playwright CLI + 本地 mock-only harness；桌面/移动端创建、编辑、筛选、计划、手动确认、运行详情；`resource_type_v2` 中文枚举多选、清空和保存请求 | 通过；24 个选项完整，空选提交 `[]`，390px 下拉无溢出，控制台 0 error，未连接真实 sidecar/GPU/TikTok |
| 最终安全复核 | 发布状态机、竞态、凭据、关闭流程与公开 DTO 复核 | 通过；无未关闭 P0/P1 |
| 生产关闭默认验收 | 三重门禁均为 0、模板/run/task/material ledger 均为空，公开页面与 release 哈希一致，旧 TT PID 未变化 | 通过；未创建模板或触发真实发布 |

## 已覆盖的高风险行为

- 启用时间与模板版本在自动建 run 时原子复核；停用、编辑或陈旧调度快照不能补建任务。
- 手动执行幂等重放返回同一 run；黑名单和账号依赖不会在同一幂等请求中重复创建副作用。
- Decimal 高精度聚合、完整北京时间日、generation 保留、两层筛选与稳定排序均有用例。
- 素材冻结后永久保留；出现 `publish_id` 或未知发布结果后只允许 reconcile。
- 调度 tick 与耗时 worker 分离；tick 失败不阻止已排队任务执行，账号串行边界仍由账本控制。
- 浏览器公开 DTO 不含源素材 URL、准备后 URL或黑名单明细，只保留安全摘要和受信 TikTok 发布链接。
- 示例 bearer 会在主 API、sidecar 启动和 runner 三处拒绝；sidecar 停止会等待在途 HTTP 工作线程。
- 浏览器在 1440px 桌面和 390px 移动视口验证；移动端无页面横向溢出，运行详情优先展示任务冻结的账号名称。
- `resource_type_v2` 空数组和字段缺省均归一化为空数组且筛选不限制类型；仅接受 `0`、`1`–`22`、`100`，`-1` 和未知编号均被拒绝。
- 发布文案可不包含 `{{content_id}}` / `{{contect_id}}`；前端不再阻止提交，服务端已验证固定文案、`{desc}` / `{url}` 及 `{code}` 模板均可创建。`{code}` 在 GPU prepare 前冻结为唯一四位码，确定失败后的发布重试仍复用完全相同的 caption；空模板、未知宏和超长文案仍保持拒绝。
- 四位码 broker 离线测试验证共享 `tt_post_code_route` 全局命名空间、自动任务高位 ID 隔离、同任务幂等、路由事实冲突关闭、published 状态克隆和禁止状态回退；测试期间 `tt_post_queue` 始终为 0。

## 真实发布声明

最初关闭默认验收未触发真实 TT 发布。`2026-08-06 18:02 +08:00` 后，用户已另行明确授权账号 640 的生产内测真实任务；该任务的精确执行与 reconcile 证据记录如下。

## 2026-08-06 `{code}` 生产验收

- 部署提交 `5b18d1ef68614ae01bf97a7e092bcd0d9c345d3f`；新 `127.0.0.1:18832/health` 与原 `127.0.0.1:18831/health` 均返回 200。
- 生产 release 上只读归一化验证包含 `{code}`、`{desc}`、`{url}` 的模板可通过；未调用创建模板 API。
- 三重门禁保持关闭；自动模板、run、task、素材账本以及自动高位码命名空间均为 0，因此未触发真实 GPU/TikTok 行为。
- 共享码路由总数部署前后均为 52，证明关闭模式验收没有消耗四位码；旧 TT release/PID 未变化。
- 最终重启窗口的一轮 scheduler/runner 失败后，连续 3 轮自然执行成功，最终结果为 success/0；runner path 为 active/waiting。

## 2026-08-06 账号 640 生产内测

- 生产服务器按 `111 + 4 + 4` 三组执行共 119 个自动发布测试，全部通过；最后一组覆盖带中文归因字段的同任务四位码幂等重放。
- 三个增量修复均先提交、推送 GitHub，再从精确提交构建不可变 release；Git blob 校验 829 个文件一致。最终 release 为 `2a1674d98cbb245bab0d4c0a6e83dbda092a6e69`。
- 任务 3 只冻结一次素材 `6013146`、剧 `peKST2RMpC`、GPU job 和最终文案；准备结果为 13,019,687 bytes、96.767 秒，重试持续复用四位码 `Q66Y`。新 broker 的真实 loopback HTTP 重放返回同一码，未写旧 TT 队列表。
- 截至 `18:36:03 +08:00`，任务 3 共 4 次发布阶段领取，前三次分别暴露四位码 Unicode 重试问题和账号 Token 到期；当前状态 `retry_wait`，错误 `tt_account_source_unavailable`，下一次为 `18:41:03 +08:00`。
- 账号快照只读核验：账号仍为 active、账号状态和 Token 状态字段均为 2、`disable_publish=0`，但 `token_expires_time=2026-08-06 18:30:02 +08:00`；发布凭据查询已返回空。因此当前没有 `publish_id`、没有未知结果，也没有调用 TikTok publish init，不能绕过 Token 有效期。
- 三重生产门禁继续为开；模板 1 继续停用，避免定时产生新任务。旧 TT PID `3055551` 未变化。
