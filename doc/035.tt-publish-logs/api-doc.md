# TT 发布日志接口

## GET `/api/admin/tt-auto-publish/publish-logs`

只读聚合素材池发布任务与自动模板账号任务。需要 `ttAutoPublishRuns` 导航权限。

### 查询参数

| 参数 | 说明 |
| --- | --- |
| `publish_source` | 空、`material_pool`、`auto_template` |
| `trigger_type` | 空、`scheduled`、`direct_test`、`auto`、`manual` |
| `source_account_id` | TikTok 账号 ID |
| `template_id` | 自动发布模板 ID；素材池来源不会命中 |
| `material_id` | 素材 ID |
| `content_id` | Drama ID / `contect_id` |
| `status` | 统一状态组 |
| `from` / `to` | 上海时区日期，`YYYY-MM-DD` |
| `limit` / `offset` | 默认 50/0，最大 200/10,000 |

### 核心响应字段

```json
{
  "ok": true,
  "items": [{
    "publish_source": "material_pool",
    "trigger_type": "scheduled",
    "task_key": "material_pool:automatic:57",
    "task_id": 57,
    "task_at_utc": "2026-08-06T06:24:00+00:00",
    "status": "published",
    "status_group": "published"
  }],
  "pagination": {"limit": 50, "offset": 0, "total": 1},
  "summary": {"total": 1, "scheduled": 0, "processing": 0, "needs_review": 0, "published": 1, "failed": 0}
}
```

响应不返回 `source_media_url`、`prepared_media_url`、claim token 或任何账号凭据。

## 错误语义

- 非法来源、触发方式、状态、日期、ID 或分页参数返回 `400 invalid_request`。
- 默认查询同时读取两个账本；任一账本不可用时失败关闭，不返回半份聚合结果。
- 明确指定单一来源时，只读取该来源；未被请求的另一个账本不可用不会阻塞。

## 兼容性

- 不新增或修改数据库字段，`publish_source`、`trigger_type`、`status_group` 和 `task_key` 均为读取时派生字段。
- 旧素材池事件、取消、人工核对仍调用原 TT Post 接口；自动任务详情复用原运行详情接口。
