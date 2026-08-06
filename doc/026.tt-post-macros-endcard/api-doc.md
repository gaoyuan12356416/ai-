# API 文档

## 状态与边界

- 本需求不新增公开路由，只扩展现有 TT 素材池/queue 的字段和校验。
- 管理端接口继续走既有管理员鉴权；内部接口继续要求 loopback/内部 Bearer token。
- 本轮验收只允许 preview、只读查询、preparation claim/process 和 GPU prepare。禁止 publish、canary、run-now 和 schedule 写入。
- 下列示例中的 ID、URL、hash 和时间均为示意，不是生产凭据或真实 Post 记录。

## 接口列表

| 方法 | 路径 | 用途 | 本需求变化/本轮权限 |
| --- | --- | --- | --- |
| POST | `/api/admin/tt-posts/materials/preview` | 后端解析素材真实元数据 | 响应增加/确认 `description`；允许 |
| POST | `/api/admin/tt-posts/material-pool` | 冻结素材并进入异步制作 | 支持模板 `{desc}`/`{url}`；请求禁止 description；只允许隔离测试 |
| GET | `/api/admin/tt-posts/material-pool` | 查询 intake + recurring pool | 返回 frozen description、准备/可用分状态；允许只读 |
| GET | `/api/admin/tt-posts/queue` | 查询 queue | 返回最终 caption、description、short-link 字段；允许只读 |
| POST | `/api/admin/tt-posts/queue` | 兼容旧的显式排队入口 | 同样由后端解析/freeze description；本轮禁止生产调用 |
| GET/POST | `/api/admin/tt-posts/schedule` | 查询/保存每日排期 | 语义不变；本轮只允许 GET |
| POST | `/api/admin/tt-posts/run-now` | 立即消费 ready 素材 | 语义不变；本轮禁止 |
| POST | `/internal/tt-posts/preparations/claim` | 后台领取 intake 制作任务 | frozen description 随 item 返回；隔离环境允许 |
| POST | `/internal/tt-posts/preparations/{id}/renew` | 续租制作任务 | 无新增公开语义；隔离环境允许 |
| POST | `/internal/tt-posts/preparations/{id}/process` | 调 GPU prepare 并完成 pool | description 从 intake 复制到 pool；隔离环境允许 |
| GET | GPU `/health` | 查看 mode/profile/eligibility/storage | `direct_outro` 可见；允许 |
| POST | GPU `/internal/tt-post/prepare` | 制作 `direct_outro` 成片 | 独立 profile；prepare-only 允许 |
| POST | GPU `/internal/tt-post/publish`、`/canary-publish` | 创建真实 TikTok Post | 本轮严格禁止 |

## 通用宏合同

允许且区分大小写：

```text
{{contect_id}}
{{content_id}}
{desc}
{url}
```

- `{desc}` 只取后端解析的 `ads_drama_resource.desc`；客户端没有可写 description 字段。
- `{url}` 在 queue 拥有唯一短链后完成渲染。
- 只做一次非递归替换。
- final caption 按 UTF-16 code units 计算，必须为 1..2200；超限不截断。

推荐模板：

```text
Watch the full story in the app 🎬

{desc}

Drama ID: {{content_id}}

{url}
```

## 请求/响应

### 1. 预览真实素材

`POST /api/admin/tt-posts/materials/preview`

请求：

```json
{
  "material_id": "5801636"
}
```

响应关键字段：

```json
{
  "item": {
    "material_id": "5801636",
    "content_id": "LZ4b4w5k3h",
    "source_media_url": "https://source.example/material.mp4",
    "material_name": "material-name",
    "drama_name": "drama-name",
    "material_language": "en",
    "material_tag": "tag",
    "description": "A frozen drama description",
    "status": "validated",
    "preparation_status": "not_started",
    "publish_ready": false
  },
  "gates": {}
}
```

此处的 description 供页面逐素材预览；真正写入时后端会再次解析，不能把 preview 响应回传为权威字段。

### 2. 加入素材池并冻结

`POST /api/admin/tt-posts/material-pool`

允许的请求字段只有：`idempotency_key`、`source_account_id`、`material_id`、`content_id`、`caption_template`、兼容字段 `caption_text`、`consent`。任何 `description` 字段均视为未知字段并拒绝。

请求：

```json
{
  "idempotency_key": "tt-pool-5801636-20260803-001",
  "source_account_id": "640",
  "material_id": "5801636",
  "content_id": "LZ4b4w5k3h",
  "caption_template": "Watch the full story in the app 🎬\n\n{desc}\n\nDrama ID: {{content_id}}\n\n{url}",
  "consent": {
    "accepted": true,
    "version": "tt-post-consent-v2",
    "accepted_at": "2026-08-03T04:00:00Z"
  }
}
```

响应关键字段：

```json
{
  "item": {
    "id": 101,
    "source_account_id": "640",
    "material_id": "5801636",
    "content_id": "LZ4b4w5k3h",
    "description": "A frozen drama description",
    "caption_template": "Watch the full story in the app 🎬\n\n{desc}\n\nDrama ID: {{content_id}}\n\n{url}",
    "caption_text": "Watch the full story in the app 🎬\n\nA frozen drama description\n\nDrama ID: LZ4b4w5k3h\n\n{url}",
    "preparation_status": "queued",
    "publish_ready": false,
    "pool_item_type": "intake"
  },
  "available_material_count": 0,
  "preparation_wakeup_requested": true,
  "preparation_timer_fallback_seconds": 60,
  "gates": {}
}
```

intake/pool 阶段的 `caption_text` 可以保留尚无 queue identity 的 `{url}`；它不是最终可发布 caption。`{desc}` 已按 frozen description 渲染。

### 3. 查询素材池

`GET /api/admin/tt-posts/material-pool?source_account_id=640&page=1&page_size=20`

响应同时包含 intake 和 ready pool item：

```json
{
  "items": [
    {
      "material_id": "5801636",
      "description": "A frozen drama description",
      "preparation_status": "ready",
      "publish_ready": true,
      "pool_item_type": "ready",
      "prepared_media_url": "https://socialkit-cdn.yingliang.tech/tt-post-prepared/aa/<sha>.mp4",
      "preparation_profile": "tt-post-direct-outro-hevc-720x1280-v1"
    }
  ],
  "summary": {
    "available": 1,
    "preparing": 0,
    "ready": 1
  },
  "gates": {}
}
```

`available` 与 `preparing` 是不同状态，UI 不得把制作中素材计为可立即发布。

### 4. queue 最终 caption

`GET /api/admin/tt-posts/queue?page=1&page_size=20`

含 `{url}` 的 queue 在冻结时已有稳定 `short_link_id/short_url`，final caption 不再含宏：

```json
{
  "items": [
    {
      "id": 201,
      "material_id": "5801636",
      "content_id": "LZ4b4w5k3h",
      "description": "A frozen drama description",
      "caption_template": "Watch the full story in the app 🎬\n\n{desc}\n\nDrama ID: {{content_id}}\n\n{url}",
      "caption_text": "Watch the full story in the app 🎬\n\nA frozen drama description\n\nDrama ID: LZ4b4w5k3h\n\nhttps://gy.g2flow.com/s2l/8000000000000000201.html",
      "short_link_id": 8000000000000000201,
      "short_url": "https://gy.g2flow.com/s2l/8000000000000000201.html",
      "long_url": "",
      "publish_mode": "hold"
    }
  ]
}
```

`long_url` 与 wrapper 按既有安全流程在发布 claim 后、调用 TikTok 前原子物化；prepare-only 不走此步骤。实现可以在隔离测试中直接验证 link builder/writer，但本轮不得借此触发发布。

### 5. 关闭每日自动排期

`POST /api/admin/tt-posts/schedule`

关闭请求必须可以只带：

```json
{
  "source_account_id": "640",
  "enabled": false,
  "expected_version": 2
}
```

关闭路径不需要 `publish_time(s)`、timezone、consent、creator-info 或账号发布设置；成功后保留原时间和历史，只递增版本并返回 `enabled=false`。版本过期返回 409。本轮只做离线/接口单测，不对生产发送此请求。

### 6. 后台制作 claim/process

领取：

```http
POST /internal/tt-posts/preparations/claim
```

```json
{
  "worker_id": "tt-prepare-runner-01",
  "lease_seconds": 300
}
```

处理：

```http
POST /internal/tt-posts/preparations/101/process
```

```json
{
  "claim_token": "<redacted>"
}
```

处理成功必须把 intake 的 frozen description 原样复制到 recurring pool，并记录 `prepared_media_url/output_sha256/output_size/duration/profile`。

### 7. GPU health

`GET /health`

`direct_outro` 预期关键字段：

```json
{
  "status": "ok",
  "media_mode": "direct_outro",
  "profile": "tt-post-direct-outro-hevc-720x1280-v1",
  "direct_post_eligible": true,
  "brand_overlay_review_required": false,
  "transition": "phone-match-0.9s",
  "storage_backend": "cos",
  "storage": {}
}
```

`direct_post_eligible=true` 不代表发布 gate 自动开启。

### 8. GPU prepare-only

`POST /internal/tt-post/prepare`

请求 mode 由 GPU 环境固定，客户端不能用请求体切换。推荐同时传 source fingerprint：

```json
{
  "job_id": "tt-prepare-only-20260803-a001",
  "content_id": "LZ4b4w5k3h",
  "expected_profile": "tt-post-direct-outro-hevc-720x1280-v1",
  "source_url": "https://source.example/immutable/material.mp4",
  "source_sha256": "<64-lowercase-hex>",
  "source_size": 12345678,
  "source_trim_tail_seconds": 0
}
```

响应关键字段：

```json
{
  "status": "ready",
  "job_id": "tt-prepare-only-20260803-a001",
  "content_id": "LZ4b4w5k3h",
  "profile": "tt-post-direct-outro-hevc-720x1280-v1",
  "direct_post_eligible": true,
  "brand_overlay_review_required": false,
  "output_url": "https://socialkit-cdn.yingliang.tech/tt-post-prepared/aa/<sha>.mp4",
  "output_sha256": "<64-lowercase-hex>",
  "output_size": 23456789,
  "probe": {
    "duration": 50.133,
    "width": 720,
    "height": 1280,
    "frame_rate": 30.0
  },
  "storage_backend": "cos",
  "reused": false
}
```

响应 `output_url` 必须与请求 `source_url` 不同。`media_mode`、outro/logo/source 指纹和 transition 以脱敏 manifest 及 health 为准。

## 错误响应

CPU 统一形状：

```json
{
  "code": "caption_desc_required",
  "message": "发布描述模板中的{desc}必须绑定有效剧描述"
}
```

## 关键错误码

| HTTP | code | 条件 |
| ---: | --- | --- |
| 400 | `invalid_request` | 未知字段（含客户端 description）、格式或类型无效 |
| 400 | `invalid_caption_template` | 模板为空、NUL 或超模板上限 |
| 400 | `caption_placeholder_invalid` | 大小写/括号/空格错误或未知宏 |
| 400 | `caption_content_id_required` | 仅旧 `caption_text` 兼容接口缺少准确 Drama ID；模板接口可不含 Drama ID 宏 |
| 400 | `caption_desc_required` | 模板含 `{desc}` 但 frozen description 为空/无效 |
| 400 | `caption_url_required` | 模板含 `{url}` 但没有合法 TT 短链 |
| 400 | `caption_length_invalid` | 最终 caption 为空或超过 2200 UTF-16 units |
| 400/409 | `tt_content_id_mismatch` | 页面 content ID 与后端真实素材映射不一致 |
| 409 | `tt_post_idempotency_conflict` | 同 idempotency key 的冻结事实不同 |
| 409 | `tt_short_link_state_invalid` / `tt_short_link_target_conflict` | 短链冻结/文件内容不一致 |
| 409 | `tt_prepared_media_matches_source` | prepared URL 与 source URL 相同 |
| 409 | `tt_prepared_media_profile_mismatch` / `prepare_profile_mismatch` | CPU/GPU profile 不一致 |
| 409 | `prepare_idempotency_conflict` | 同 job ID 的 mode/profile/source/outro/logo/trim 合同变化 |
| 409 | `tt_post_schedule_version_conflict` 或现有等价码 | 自动排期 expected_version 过期 |
| 500/502 | `prepared_media_invalid` | manifest、资产指纹或媒体探测不符合合同 |

错误后必须 fail closed：不得消费 pool、建立 Post、触发 runner 或修改 schedule。

## 兼容性说明

- `{{contect_id}}` 是历史拼写，继续支持；`{{content_id}}` 为兼容别名。
- 不含 `{desc}`/`{url}` 的历史合法模板行为不变。
- SQLite 只增列；历史已发布 queue 不回写。available 老记录含 `{desc}` 但 description 为空时必须阻断。
- `direct_clean` 仍使用 `tt-post-direct-clean-*-v1`、无片尾；`branded_preview` 仍使用 `tt-post-*-v2` 且不可正式直发。
- `direct_outro` 使用独立 `tt-post-direct-outro-*-v1`，复用既有审核的 Logo/tutorial-outro compositor。
- TT `8[0-9]{18}` 精确短链路由置于 X 通用数字规则之前；旧 X URL 不变。
- TT 成片用专用 COS 域名；其他业务继续使用原有存储配置。
