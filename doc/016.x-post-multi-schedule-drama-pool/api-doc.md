# API 文档

## 页面 API

所有接口要求登录 Cookie、对应快速导航权限、同源 JSON 写请求，并返回 `Cache-Control: no-store`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/admin/x-posts/material-pool/account-options` | 素材池可选账号 |
| GET/POST | `/api/admin/x-posts/material-pool/schedule` | 查询/保存素材池排期 |
| GET | `/api/admin/x-posts/drama-pool/account-options` | 短剧池可选账号 |
| POST | `/api/admin/x-posts/drama-pool/account-options/{account_id}/verify` | 在短剧池页面权限范围内校验并刷新一个账号；页面仅对 `refresh_required` 账号自动调用 |
| GET/POST | `/api/admin/x-posts/drama-pool/schedule` | 查询/保存短剧池排期 |
| POST | `/api/admin/x-posts/drama-pool/preview` | 只读预检短剧 ID |
| GET/POST | `/api/admin/x-posts/drama-pool` | 查询/加入短剧池 |
| GET | `/api/admin/x-posts/drama-pool/{pool_id}/episodes` | 剧集发布明细 |
| DELETE | `/api/admin/x-posts/drama-pool/{pool_id}` | 删除单条未占用短剧 |
| POST | `/api/admin/x-posts/drama-pool/batch-delete` | 原子批量删除当前页明确选中的未占用短剧 |

## 排期保存

```json
{
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "account_ids": [2, 3, 4],
  "publish_times": ["09:00", "12:30", "18:00"],
  "body_template": "🎬 {{drama_name}}\n{{desc}}\n{{url}}",
  "version": 2
}
```

- `version` 为乐观锁版本。
- 账号按提交顺序冻结；时间会去重并按 `HH:MM` 排序。
- 启用时账号和时间均不能为空。
- 同一账号不能在两个池配置同一时间。
- 素材池模板必须包含一次 `{{drama_name}}` 和 `{{desc}}`；短剧池还必须包含一次 `{{episode_number}}`。
- 两个池都可选用一次 `{{url}}`，其值为当前发布日志对应的 `https://gy.g2flow.com/s2l/{log_id}.html` 追踪短链。
- 未知宏、重复宏、控制字符、空模板和超过 2000 字符的模板返回 `invalid_post_template`。
- 保存递增配置版本；claim 和 queue 都冻结模板，因此后续编辑不会改写已经认领或已创建队列的正文。

## 短剧加入

页面只提交 `drama_ids`；后台先从源表生成逐项 `validation_checks`，再经 sidecar 原子写入。一次 1–100 个 ID，内部最大请求 5 MiB。

成功响应仅返回：

```json
{
  "items": [
    {
      "id": 1,
      "content_id": "123456",
      "status": "pending",
      "free_episode_count": 8,
      "last_error_code": "",
      "last_error_message": "",
      "created_at": "2026-07-27T10:00:00Z"
    }
  ],
  "created_count": 1,
  "available_count": 1,
  "validation_failed_count": 0,
  "audit_recorded": true
}
```

## 短剧查询与批量删除

短剧池列表每行额外返回：

```json
{
  "queue_count": 0,
  "has_history": false,
  "deletable": true,
  "delete_block_reason": ""
}
```

批量删除请求只接受 1–100 个唯一正整数池记录 ID：

```json
{
  "pool_item_ids": [2, 5, 11]
}
```

成功响应：

```json
{
  "item": {
    "items": [
      {"id": 2, "content_id": "abc", "deleted": true}
    ],
    "deleted_count": 1
  },
  "audit_recorded": true
}
```

任一记录不存在、已有 `drama_pool_item_id` 或同短剧 `content_id` 队列历史，或状态不是 `pending/validation_failed` 时整批回滚。内部接口为 `/internal/posts/drama-pool/batch-delete`，仅主后台 loopback bearer 可调用。

## 定时内部 API

仅 loopback 和 daily bearer 可访问：

| 路径 | 用途 |
| --- | --- |
| `/internal/posts/schedules/due` | 原子认领并返回到期/同日待恢复批次 |
| `/internal/posts/schedule-plan/query` | 查询冻结批次和队列 |
| `/internal/posts/schedule-plan` | 原子创建素材或短剧队列 |
| `/internal/posts/schedule-runs/record-failure` | 记录预检失败；可绑定短剧池并标记待确认 |
| `/internal/posts/material-pool/available` | 按上传时间倒序返回可用素材 |
| `/internal/posts/drama-pool/available` | 保留账号绑定，并按上传时间倒序补充未绑定短剧 |
| `/internal/posts/storage/preflight` | 数据盘/短链目录预检 |
| `/internal/posts/queue/{queue_id}/publish` | 发布既有冻结队列 |

## 主要错误码

| 错误码 | 含义 |
| --- | --- |
| `x_post_schedule_collision` | 两个池的账号和时间发生冲突 |
| `x_post_schedule_version_conflict` | 排期已被其他操作修改 |
| `x_post_schedule_slot_in_progress` | 当前 90 秒窗口内禁止修改 |
| `x_post_drama_pool_needs_review` | 前序短剧需人工确认，暂停后续 |
| `x_post_drama_sequence_conflict` | 候选没有按账号绑定、剧集顺序或新剧倒序规则提交 |
| `drama_episode_gap` | 免费集数不连续 |
| `drama_episode_url_ambiguous` | 同一集存在不一致 URL |
| `drama_metadata_ambiguous` | 短剧元数据不一致 |
| `drama_resource_invalid` | 短剧资源字段非法；响应消息附具体字段原因 |
| `x_post_schedule_stale_claim` | 跨日冻结批次已安全停止 |
| `x_post_rate_limited` | X 临时限流；自动校验停止启动后续账号并保留账号待刷新状态 |
| `x_post_drama_pool_item_not_found` | 批删中至少一个池记录不存在，整批未删除 |
| `x_post_drama_pool_item_occupied` | 批删中至少一个记录已有历史或状态不可删，整批未删除 |

## 兼容性说明

- SQLite 仅增表、增列、增索引和触发器，保留旧 daily/catch-up 数据。
- 未绑定 `schedule_run_id` 的旧队列继续使用既有唯一性合同。
- 旧页面接口保持不变；素材池新增排期区但不改变加入/预览合同。
- `ads_drama_resource.sub_number=0` 作为非剧集平台记录忽略；同集双平台行仅在媒体 URL 和其余剧元数据一致时合并。
