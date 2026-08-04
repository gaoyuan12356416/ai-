# 012.x-post-material-pool X Post 素材池 API

## 鉴权边界

- 浏览器素材池接口仅接受 Feishu Cookie，并按快速导航栏 `xPostMaterialPool` 的实时配置授权：分组/菜单必须启用，任一级 `adminOnly=true` 时要求管理员，分组和菜单声明的每个 `module` 都要求当前用户具备。API Token 始终拒绝。
- 导航配置缺失或禁用返回 403 `navigation_item_unavailable`；仅管理员门禁返回 403 `admin_required`；模块缺失返回 403 `permission_denied`；生产配置读取/解析失败返回 503 `navigation_config_unavailable`。所有拒绝响应 no-store。
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
      "material_preview_url": "https://example-cdn.invalid/material.mp4",
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

列表的 `material_preview_url` 由主后台按当前页素材 ID 批量、只读、精确查询 `ads_custom_source.url` 后附加。绝对 `http://` 地址会在内存中升级为 `https://`，不回写 `ads_custom_source`；最终仅返回无凭据、无控制字符、端口为空或 443 的 HTTPS URL。素材的 X 合规状态不影响 URL 返回，源记录或安全 URL 不存在时返回空字符串。发布筛选使用相同的 HTTP→HTTPS 规范化结果，页面直接用该字段打开素材。

## GET /api/admin/x-posts/material-pool/preview（兼容接口）

鉴权与素材池列表接口相同，按 `xPostMaterialPool` 快速导航配置授权。查询参数必须且只能包含：

| 参数 | 规则 |
| --- | --- |
| `material_id` | 1 至 19 位正整数，且该 ID 当前必须存在于 X Post 素材池 |

处理流程：

1. 通过 Sidecar 管理查询确认素材 ID 属于当前全局素材池。
2. 主后台按精确 ID 只读查询 `ads_custom_source.url`，必须唯一命中。
3. URL 必须是无用户名/密码、无控制字符、带 hostname 的 HTTPS 地址。
4. 成功返回 `302`，`Location` 为素材源 URL，并设置 `Cache-Control: no-store`、`Pragma: no-cache`、`Referrer-Policy: no-referrer` 和 `X-Content-Type-Options: nosniff`。

非法参数返回 400；素材不在池中、源记录缺失/重复或 URL 不安全返回 404 `x_post_material_preview_unavailable`；只读数据源异常返回 503。接口不返回 MySQL 凭据，不修改素材池状态、queue 或发布日志。该跳转入口仅为旧调用兼容，当前素材池页面不再使用；页面直接使用列表 DTO 的 `material_preview_url`，X Post 预览继续使用 `preview_url`。

## POST /api/admin/x-posts/material-pool

请求：

```json
{
  "material_ids": ["5221348", "5221349", "5221350"]
}
```

- 数组必须包含 1 至 100 项。
- 每项规范为正整数文本，`"00101"` 保存为 `"101"`。
- 同批重复、池内重复、已有任意 queue 历史按素材逐条跳过；同批其余全新素材仍在一个事务中写入。
- 非法 ID、校验结果缺失/冲突、数据库异常或未知唯一约束冲突仍 fail closed，并回滚本次所有待新增素材。
- 主后台先复用正式 X selector 做只读即时校验，覆盖 Dramawave、视频类型/删除态/时长、HTTPS、必填元数据、违规/内容标签审计值、短剧映射和短剧可投放时间。违规记录以及素材源、资源、短剧 labels 中的色情、裸露、暴力等内容词不拒绝 X 候选；原始计数继续进入 queue 审计字段。
- 可投放时间读取 `ads_drama_info.app_id=1479`、同 `content_id + language` 的 `deploy_time`，多端取最晚值。严格晚于当前时间时保存 `drama_not_yet_deliverable` 和北京时间说明；素材不绑定 queue，后续 daily 每次重新校验，时间到达后自动恢复候选资格。
- 素材不存在或发布所需数据标准不通过时仍可加入池，但 `availability` 立即为 `validation_failed`，页面显示“不可用”；检查服务异常统一 fail closed 为 `material_validation_unavailable`。历史 `material_has_violation`、`material_source_tag_unsafe`、`material_tag_unsafe` 错误码仅作审计，列表与 available 接口按可供发布处理。
- 素材 ID、校验时间和逐素材错误与池记录在 Sidecar 同一事务写入，不存在先显示 `available` 的窗口。
- 入池校验不替代 daily 的媒体文件下载/ffprobe 预检。
- 兼容单值 `material_id`，主页面统一发送 `material_ids`。
- 成功写入后台审计日志。

成功响应：

```json
{
  "items": [
    {"id": 141, "material_id": "5221349"},
    {"id": 142, "material_id": "5221350"}
  ],
  "requested_count": 3,
  "unique_count": 3,
  "added_count": 2,
  "created_count": 2,
  "skipped_count": 1,
  "duplicate_input_count": 0,
  "already_in_pool_count": 1,
  "already_used_count": 0,
  "skipped_items": [
    {
      "material_id": "5221348",
      "code": "x_post_pool_material_already_exists",
      "message": "素材已在X素材池中"
    }
  ],
  "available_count": 1,
  "validation_failed_count": 1
}
```

`available_count` 与 `validation_failed_count` 只统计本次实际新增记录。即使
全部素材均因可预期的重复/历史占用而跳过，接口也返回 200 和逐类汇总，
不会把它误报为整批写入异常。

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

仅 backend bearer。请求包含服务端生成的 `actor`、`scope=all`、与管理员 GET 相同的分页/筛选字段；当 actor 不是管理员时，主后台必须在完成导航配置授权后增加精确 `navigation_item="xPostMaterialPool"`。Sidecar 只在素材池 query/add/delete 三个 backend 路由识别该标记，返回素材池列表；daily bearer 不能调用这些路由。

## POST /internal/posts/material-pool/add

仅 backend bearer。请求：

```json
{
  "actor": {
    "user_id": "ou_xxx",
    "name": "Operator",
    "role": "user"
  },
  "scope": "all",
  "navigation_item": "xPostMaterialPool",
  "material_ids": ["5221348"],
  "validation_checks": [
    {
      "material_id": "5221348",
      "error_code": "",
      "error_message": ""
    }
  ]
}
```

`scope` 非 `all` 返回 403。非管理员缺失或伪造其他 `navigation_item` 返回 403；管理员为兼容现有内部管理调用可以省略该字段。`validation_checks` 必须与本批规范化后的素材 ID 一一对应；缺失时 Sidecar 不信任调用方，统一以 `material_validation_pending` 入池并显示不可用。提供了不完整、重复或越界的检查集合时整批 400 且不写入。

## POST /internal/posts/material-pool/{pool_item_id}/delete

仅 backend bearer。请求携带服务端 actor、`scope=all`，非管理员还必须携带主后台签发的精确 `navigation_item="xPostMaterialPool"`，响应为 `{"item": {...}}`。

## POST /internal/posts/material-pool/available

backend 或 daily bearer。请求：

```json
{"limit": 1000}
```

`limit` 为 1 至 1000。daily runner 传 `X_POST_DAILY_SCAN_LIMIT`，默认 1000。接口只返回主状态未发布、且无任何同池 ID/同素材 key queue 的记录，严格按 `created_at DESC, id DESC`：

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
      "error_code": "drama_not_yet_deliverable",
      "error_message": "短剧可投放时间为2026-07-28 10:00:00（北京时间），当前尚未到达"
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
- Sidecar 事务内重新校验池记录仍未发布、快照未变、未占用，且候选按上传时间倒序。
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
- `drama_not_yet_deliverable`
- `drama_deploy_time_missing`
- `drama_deploy_time_invalid`
