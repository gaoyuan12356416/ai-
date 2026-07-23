# 012.x-post-material-pool X Post 素材池 API

## 鉴权边界

- 浏览器管理接口仅接受 Feishu Cookie 管理员，不接受 API Token 或普通用户。
- 浏览器写接口同时要求同源 JSON；响应均为 `Cache-Control: no-store`。
- 主后台到 Sidecar 使用 loopback backend bearer。
- daily bearer 只允许 `available`、`check`、固定三账号 verify、正式 `daily-plan` 和对应 queue publish；不能查询、添加或删除素材池。
- 响应不返回 OAuth Token、内部 bearer、MySQL 凭据；错误说明在存储/输出前脱敏。

## GET /api/admin/x-posts/material-pool

允许查询参数：

| 参数 | 规则 |
| --- | --- |
| `page` | 正整数，默认 1 |
| `page_size` | 正整数，默认 20，最大 100 |
| `status` | `unpublished` / `published` |
| `availability` | `available` / `validation_failed` / `occupied` / `failed` / `needs_review` / `published` |
| `material_id` | 1 至 19 位正整数 |

未知参数、非法枚举和 SQL 式输入返回 400。

响应示例：

```json
{
  "items": [
    {
      "id": 12,
      "material_key": "5221348",
      "material_id": "5221348",
      "status": "unpublished",
      "availability": "available",
      "published_at": "",
      "last_checked_at": "",
      "last_error_code": "",
      "last_error_message": "",
      "created_by_user_id": "ou_xxx",
      "created_by_name": "Admin",
      "created_at": "2026-07-23T03:10:00Z",
      "updated_at": "2026-07-23T03:10:00Z",
      "queue_id": null,
      "run_id": null,
      "run_date": null,
      "account_id": null,
      "account_username": null,
      "queue_status": null,
      "publish_status": "",
      "unknown_outcome": false,
      "preview_url": "",
      "publish_error_code": "",
      "publish_error_message": ""
    }
  ],
  "summary": {
    "total": 1,
    "unpublished": 1,
    "published": 0,
    "available": 1,
    "occupied": 0
  },
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1,
    "pages": 1
  }
}
```

`summary.available` 必须与 `availability=available` 相同口径，不包含 `validation_failed`。

## GET /api/admin/x-posts/material-pool/preview

仅接受 Feishu Cookie 管理员。查询参数必须且只能包含：

| 参数 | 规则 |
| --- | --- |
| `material_id` | 1 至 19 位正整数，且该 ID 当前必须存在于 X Post 素材池 |

处理流程：

1. 通过 Sidecar 管理查询确认素材 ID 属于当前全局素材池。
2. 主后台按精确 ID 只读查询 `ads_custom_source.url`，必须唯一命中。
3. URL 必须是无用户名/密码、无控制字符、带 hostname 的 HTTPS 地址。
4. 成功返回 `302`，`Location` 为素材源 URL，并设置 `Cache-Control: no-store`、`Pragma: no-cache`、`Referrer-Policy: no-referrer` 和 `X-Content-Type-Options: nosniff`。

非法参数返回 400；素材不在池中、源记录缺失/重复或 URL 不安全返回 404 `x_post_material_preview_unavailable`；只读数据源异常返回 503。接口不返回 MySQL 凭据，不修改素材池状态、queue 或发布日志。页面中的 X Post 预览继续使用列表 DTO 的 `preview_url`，与本接口分列展示。

## POST /api/admin/x-posts/material-pool

请求：

```json
{
  "material_ids": ["5221348", "5221349", "5221350"]
}
```

- 数组必须包含 1 至 100 项。
- 每项规范为正整数文本，`"00101"` 保存为 `"101"`。
- 同批重复、池内重复、已有任意 queue 历史时，整个事务回滚。
- 兼容单值 `material_id`，主页面统一发送 `material_ids`。
- 成功写入后台审计日志。

成功响应：

```json
{
  "items": [],
  "created_count": 3
}
```

## DELETE /api/admin/x-posts/material-pool/{pool_item_id}

请求体为 `{}`，仅能删除未发布且不存在任何同 `pool_item_id` 或同 `material_key` queue 的记录。

成功响应由 Sidecar 包装为：

```json
{
  "item": {
    "id": 12,
    "material_id": "5221348",
    "status": "unpublished",
    "deleted": true
  }
}
```

已发布返回 `x_post_pool_item_published`，已占用返回 `x_post_pool_item_occupied`。

## POST /internal/posts/material-pool/query

仅 backend bearer。请求包含服务端生成的 `actor`、`scope=all` 和与管理员 GET 相同的分页/筛选字段，返回素材池列表。

## POST /internal/posts/material-pool/add

仅 backend bearer。请求：

```json
{
  "actor": {
    "user_id": "ou_xxx",
    "name": "Admin",
    "role": "admin"
  },
  "scope": "all",
  "material_ids": ["5221348"]
}
```

`scope` 非 `all` 返回 403。

## POST /internal/posts/material-pool/{pool_item_id}/delete

仅 backend bearer。请求携带服务端 actor 和 `scope=all`，响应为 `{"item": {...}}`。

## POST /internal/posts/material-pool/available

backend 或 daily bearer。请求：

```json
{"limit": 1000}
```

`limit` 为 1 至 1000。daily runner 传 `X_POST_DAILY_SCAN_LIMIT`，默认 1000。接口只返回主状态未发布、且无任何同池 ID/同素材 key queue 的记录，严格按 `created_at ASC, id ASC`：

```json
{
  "items": [
    {
      "id": 12,
      "material_key": "5221348",
      "material_id": "5221348",
      "created_at": "2026-07-23T03:10:00Z"
    }
  ]
}
```

该接口不把 `last_error_code` 当永久禁用；校验失败素材可在后续批次重新检查。

## POST /internal/posts/material-pool/check

backend 或 daily bearer。请求 1 至 100 条互异池 ID：

```json
{
  "checks": [
    {
      "pool_item_id": 12,
      "error_code": "material_has_violation",
      "error_message": "material has a violation record"
    }
  ]
}
```

只更新仍未发布且未绑定 queue 的池记录；已占用/已发布记录跳过。daily runner 对该接口采用 best effort，接口失败不能改变 queue 排重语义。

单次 `checks` 最多 100 条。runner 对较大扫描结果按 100 条切分；例如 205 条发送 100/100/5 三次，避免因 400 导致全部检查审计丢失。

成功响应：

```json
{"item":{"updated_count":1}}
```

## POST /internal/posts/daily-plan

既有接口的素材池增量合同：

- daily bearer 必须一次提交恰好三条候选。
- 每条必须包含互异的 `pool_item_id`、匹配的 `material_key` 和原始 `pool_created_at`。
- Sidecar 事务内重新校验池记录仍未发布、快照未变、未占用，且三条按 FIFO 正序。
- 任何一项失败，run/三条 queue 全部回滚。
- backend bearer 保留 legacy 管理能力；正式 daily bearer 强制 `require_pool=true`。

## 稳定错误码

- `invalid_request`
- `x_admin_required`
- `x_post_pool_item_not_found`
- `x_post_pool_item_occupied`
- `x_post_pool_item_published`
- `x_post_pool_item_unavailable`
- `x_post_pool_material_already_exists`
- `x_post_pool_material_already_used`
- `x_post_pool_required`
- `x_post_material_already_used`
- `x_post_account_day_already_reserved`
- `x_post_daily_candidate_shortage`
- `x_post_daily_run_exists`
- `x_post_storage_conflict`
