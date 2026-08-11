# 038.x-post-priority-manual-publish API

## 鉴权

- 浏览器管理 API 仅接受 Feishu Cookie、实时 `xPostMaterialPool`/`xPostDramaPool` 导航权限和同源 JSON 写请求；API Token 拒绝。
- loopback 接口继续使用内部 bearer。`backend` 可代表已授权操作者创建/查询请求；`daily` 只能领取并处理已创建手动批次。
- 所有响应和日志禁止包含 OAuth token、内部 bearer、数据库凭据或原始上游响应头。

## PUT /api/admin/x-posts/drama-pool/{pool_item_id}/priority

请求：

```json
{"high_priority": true}
```

成功返回更新后的短剧池安全 DTO。仅未分配、未完成、无错误且仍有免费集数的记录可设置；状态竞争返回 `409 x_post_drama_priority_conflict`。

## POST /api/admin/x-posts/material-pool/manual-publish

请求：

```json
{
  "material_ids": ["5221348", "5221349"],
  "account_ids": [101, 102],
  "idempotency_key": "x-post-manual-ui-<uuid>"
}
```

规则：

- 两个数组长度必须相同且为 1～50；各自内部不得重复。
- 服务端只信任 ID；账号快照、会员资格、素材元数据、描述模板和合规证据由服务端读取。
- 成功返回 HTTP 202。相同幂等键和相同输入返回同一批次；同键不同输入返回 409。

响应示例：

```json
{
  "item": {
    "id": 12,
    "status": "queued",
    "expected_count": 2,
    "queued_count": 0,
    "published_count": 0,
    "failed_count": 0,
    "unknown_count": 0,
    "material_ids": ["5221348", "5221349"],
    "account_ids": [101, 102],
    "created": true
  }
}
```

## GET /api/admin/x-posts/material-pool/manual-runs/{manual_run_id}

返回批次安全状态、脱敏错误和队列摘要。不会返回素材 URL、完整 Post 文案、凭据或 token。

## 内部 worker 接口

- backend bearer：`POST /internal/posts/manual-runs/create`
- backend bearer：`POST /internal/posts/manual-runs/{id}/query`
- backend bearer：`POST /internal/posts/drama-pool/{id}/priority`
- `POST /internal/posts/manual-runs/claim`
- `POST /internal/posts/manual-plan`
- `POST /internal/posts/manual-runs/record-failure`
- 继续使用 `POST /internal/posts/queue/{queue_id}/publish` 发布冻结队列。

后三条 worker 写路由只接受 `daily` bearer；创建、查询和高优路由只接受 backend bearer。`daily` 不能创建管理员请求、读取管理员查询接口或改变高优。
