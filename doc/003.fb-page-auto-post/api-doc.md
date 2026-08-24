# API 文档

## 接口列表

主 API 均要求 Feishu Cookie、`fb_page_posts` 权限、no-store；写接口还要求同源 JSON。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/admin/fb-auto-publish/groups` | 权限内组及Page计数 |
| GET/POST | `/api/admin/fb-auto-publish/templates` | 列表/创建 |
| GET/POST | `/api/admin/fb-auto-publish/templates/{id}` | 详情/按版本更新 |
| POST | `/api/admin/fb-auto-publish/templates/{id}/{enable,disable,run-now}` | 启停/异步手动计划 |
| GET | `/api/admin/fb-auto-publish/runs` | 运行列表 |
| GET | `/api/admin/fb-auto-publish/runs/{id}` | Page任务明细 |
| GET | `127.0.0.1:18835/health` | 无秘密健康信息 |

内部 bearer 路由：`POST /internal/fb-auto-post/{tick,plan-next,prepare-next,execute-next,reconcile-next}`。

## 请求/响应

模板字段：`name/group_ids/language/message_template/video_template/metric_window_days/drama_launch_window_days/cooldown_days/drama_rule/material_rule/schedule`。`video_template` 必须为 `random_overlay`。`language` 保存数据库规范小写 code，常见名称会归一（`english→en`），支持 `zh-tw`。服务端按组派生并冻结 `app_id/product/material_data_source/metric_product/metric_platform`，不信任浏览器产品或来源文本。

`message_template` 允许 `{{drama_name}}`、`{{material_name}}`、`{{content_id}}`、`{{desc}}`、`{{url}}`。`{{desc}}` 来自同 app/content/language 的短剧资源描述；`{{url}}` 可选且最多一次，展开为 `https://gy.g2flow.com/s2l/fb/{task_id}.html`。对应 W2A long URL 固定使用 `https://www.dramawavew2a.com/ads/0/2049/view` 与 `af_channel=AIpost`；short/long/message 在任务创建事务中冻结。

`run-now` 请求为 `{"expected_version":N,"operation_id":"客户端生成的16-100字符幂等ID"}`，只写入 manual due-slot 并返回 `202`、`due_slot_id/status/operation_id/idempotent`；同模板版本+operation_id 重试不重复创建。Page/素材查询、GPU 和 Graph 均不在主 API 请求内执行。

内部路由：`tick` 只持久化未来 due slots；`plan-next` 冻结 Page、指标代次和源素材；`prepare-next` 仅调用独立 GPU 制作；`execute-next` 仅领取到时 ready 任务；`reconcile-next` 只读查询已返回 ID 的 Graph 对象。

组 DTO 含 `group_id/name/group_type/group_label/app_id/product/total_pages/publishable_pages/missing_token_pages`。可发布 Token 的当前口径是 `status<>1` 且 Token 非空；计划统计与每次 execute/reconcile 都实时读取，不缓存授权状态。运行汇总含 total/publishable/missing/overlap/queued/skipped。

## 错误码

| 错误码 | 中文含义 |
| --- | --- |
| `fb_auto_owner_mapping_missing` | 未唯一映射负责人 |
| `fb_auto_legacy_queue_conflict` | 旧启用队列占用组 |
| `fb_auto_group_template_conflict` | 新模板占用组 |
| `fb_auto_page_template_conflict` | 不同组有重复Page |
| `fb_auto_previous_run_backlog` | 前一时隙未完成 |
| `fb_auto_enabled_template_edit_denied` | 已启用模板须先停用再编辑 |
| `fb_auto_page_pool_empty/unpublishable` | 空组/零可发布Page |
| `fb_page_missing_eligible_token` | 执行时没有非被封且非空的Token |
| `fb_auto_no_eligible_video` | 无合格视频 |
| `fb_auto_video_template_required` | 视频制作模板缺失或枚举不支持 |
| `fb_auto_metric_window_not_ready` | 指标窗口缺完整 READY 日 |
| `fb_auto_capacity_exceeded` | 可发布 Page、日任务或模板数量超过门禁 |
| `fb_auto_capacity_snapshot_changed` | 启用模板集合在容量校验期间发生变化 |
| `fb_auto_catalog_scan_timeout/too_large` | 完整素材目录超过整体时间或分页安全边界 |
| `fb_auto_product_mapping_unsupported` | 目标 Page 池不在受控产品映射 |
| `fb_auto_prepared_media_required` | 缺独立 GPU 成片，禁止 Graph |
| `fb_auto_message_length_invalid` | 宏展开后文案超过5000字符，未创建任务 |
| `fb_auto_link_metadata_invalid` | 构造TT兼容归因所需的Page/素材字段无效 |
| `fb_auto_short_link_snapshot_invalid` | task短链、长链或文案快照不一致，禁止Graph |
| `fb_auto_short_link_root_invalid/write_failed/conflict` | 公共短链目录无效、写入失败或ID目标冲突，禁止Graph |
| `fb_graph_*_outcome_unknown` | 结果未知，禁止重发 |
| `fb_graph_reconcile_all_credentials_rejected` | 全部健康授权均明确无法对账，终态人工确认 |

冲突响应只含安全 ID/name/status/count，不含旧条件或 Token。`submitted` 只会继续调用 Graph GET；`unknown`、`failed_without_retry` 及任何已有 Graph ID 的任务均禁止再次 POST。

## 兼容性说明

接口、权限和 SQLite 均为加法式；`fb_auto_task` 只新增 `short_url/long_url`。不改旧队列、X/TT 路由和 MySQL 数据。Graph 默认与现有服务器脚本对齐为可配置 `v22.0`。
