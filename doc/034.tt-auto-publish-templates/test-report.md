# 测试报告

## 状态

自动化测试、旧 TT 回归、真实浏览器无发布验收和最终安全复核已通过；生产关闭默认验收须在后续明确授权部署后执行。

## 结果

| 范围 | 命令/方法 | 结果 |
| --- | --- | --- |
| 新系统单元、契约与 UI 测试 | `python -m unittest scripts.test_tt_auto_post_store scripts.test_tt_auto_post_selector scripts.test_tt_auto_post_metrics scripts.test_tt_auto_publish_ui scripts.test_tt_auto_post_service scripts.test_tt_auto_post_publisher scripts.test_tt_auto_post_links scripts.test_tt_auto_publish_app_contract scripts.test_tt_auto_post_runner -v` | 112/112 通过 |
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
- 发布文案可不包含 `{{content_id}}` / `{{contect_id}}`；前端不再阻止提交，服务端已验证只含 `{desc}` / `{url}` 的模板可以创建并原样冻结。空模板、未知宏、超长文案和 `{code}` 仍保持拒绝。

## 真实发布声明

本次开发与部署验收不得触发真实 TT 发布。若后续授权 canary，必须另附精确账号、素材、时间和 reconcile 证据。
