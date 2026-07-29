# API 文档

## 接口列表

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/admin/tt-posts/accounts` | 安全账号列表和数据库候选状态 | `ttPostPool` |
| POST | `/api/admin/tt-posts/creator-info` | GPU 实时账号能力预检 | `ttPostPool` + 同源 |
| POST | `/api/admin/tt-posts/materials/preview` | 单个素材、Drama ID和 GPU 成片预览；页面批量时逐条调用 | `ttPostPool` + 同源 |
| GET | `/api/admin/tt-posts/queue` | 查询发布任务 | `ttPostPool` |
| POST | `/api/admin/tt-posts/queue` | 冻结并创建任务 | `ttPostPool` + 同源 |
| POST | `/api/admin/tt-posts/queue/{id}/cancel` | 取消 init 前任务 | `ttPostPool` + 同源 |
| POST | `/api/admin/tt-posts/queue/{id}/reconcile` | 人工核对 GPU 账本中的已有结果 | `ttPostPool` + 同源 |
| GET | `/api/admin/tt-posts/events?queue_id={id}` | 查询只追加事件 | `ttPostPool` |

内部 GPU 端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 不含密钥的健康状态 |
| POST | `/internal/tt-post/creator-info` | 调用 TikTok `creator_info` 并返回安全 DTO |
| POST | `/internal/tt-post/prepare` | 下载正片、拼片尾、动态 Drama ID、探测和固化 |
| POST | `/internal/tt-post/publish` | 三重门禁通过后执行 Direct Post init |
| POST | `/internal/tt-post/reconcile` | 按现有 `publish_id` 查询状态 |

## 页面批量编排合同

本次不新增批量 API 或数据库批次表。页面完成以下编排：

1. textarea 接受 1–100 个正整数素材 ID，支持空白、换行、中英文逗号和中英文分号分隔。
2. 页面按首次出现顺序规范化、去重；任一 token 非法时，在调用后台前整体拒绝。
3. 页面对每个唯一 ID 依次调用现有 `POST /materials/preview`。单项失败写入页面失败明细，继续下一项。
4. 成功预览项按输入顺序组成排期列表。第一项使用用户输入的上海时间，之后每项增加同一间隔；间隔为 1–1440 分钟整数，默认 10 分钟。预览失败项不占时间槽。
5. 页面对每个成功预览项依次调用现有 `POST /queue`。每项使用独立且稳定的幂等键；本批确认时间在首次保存时冻结，原批重试不重新生成。某项失败不回滚此前成功项，也不阻断随后项。建队失败会留下该时间槽，后续项不前移。
6. 页面最终展示预览成功/失败、建队成功/失败数量和逐项结果。

因此 HTTP 层仍只返回单项 `item`；所谓“批量结果”是浏览器对多次单项响应的安全汇总。

## 请求/响应

单个素材预览：

```json
{
  "material_id": "5824343"
}
```

预览与随后建队基于素材 ID、真实 `content_id`、源地址/指纹、裁剪秒数和媒体 profile 生成同一确定性 prepare job 身份。源或 profile 未变化时，GPU 复用既有完成产物；任一身份字段变化时生成新 job。

创建单条任务：

```json
{
  "idempotency_key": "tt-post:019fa...",
  "material_id": "5824343",
  "content_id": "Y9v1yQcFqM",
  "source_account_id": "700",
  "scheduled_at": "2026-07-30T02:00:00.000Z",
  "timezone": "Asia/Shanghai",
  "caption_template": "Watch the full story in the app 🎬\n\nDrama ID: {{contect_id}}\n\nVisit my profile → Open the link → Search the Drama ID → Watch now.",
  "privacy_level": "SELF_ONLY",
  "allow_comment": false,
  "allow_duet": false,
  "allow_stitch": false,
  "commercial_disclosure": false,
  "brand_organic_toggle": false,
  "brand_content_toggle": false,
  "is_aigc": false,
  "publish_mode": "hold",
  "consent": {
    "accepted": true,
    "version": "tt-direct-post-consent-20260729",
    "accepted_at": "2026-07-29T07:00:00.000Z"
  }
}
```

`caption_template` 是当前页面的可编辑模板。它必须至少包含
`{{contect_id}}` 或 `{{content_id}}`，且不得包含其他未知占位符。服务端
重新解析素材真实 `content_id` 后渲染并冻结 `caption_template` 与最终
`caption`；最终描述不得超过 2200 个 UTF-16 单位。请求中的 `content_id`
仅用于核对页面预览身份，不能覆盖服务端真实映射。

兼容旧调用方：

- 只提交旧字段 `caption_text`：按已经渲染的单素材描述处理，并要求其中的 Drama ID 与真实 `content_id` 一致。
- `caption_template`、`caption_text` 均省略：使用当前默认模板。
- 两字段同时提交：`caption_text` 必须与 `caption_template` 按真实 Drama ID 渲染的结果逐字一致。
- 旧任务以原始幂等键和原始请求精确重放：直接返回既有冻结任务，不修改历史模板/描述，不再次调用 GPU。

成功响应只返回安全字段：

```json
{
  "item": {
    "id": 1,
    "material_id": "5824343",
    "content_id": "Y9v1yQcFqM",
    "source_account_id": "700",
    "account_name_snapshot": "Dramawave Short Dramas",
    "scheduled_at": "2026-07-30T02:00:00Z",
    "status": "scheduled",
    "publish_mode": "hold",
    "caption_template": "Watch the full story in the app 🎬\n\nDrama ID: {{contect_id}}\n\nVisit my profile → Open the link → Search the Drama ID → Watch now.",
    "caption_text": "Watch the full story in the app 🎬\n\nDrama ID: Y9v1yQcFqM\n\nVisit my profile → Open the link → Search the Drama ID → Watch now."
  },
  "gates": {
    "live_enabled": false,
    "audit_approved": false,
    "url_property_verified": false,
    "is_open": false
  }
}
```

账号列表禁止包含 `access_token`、`refresh_token`、Authorization 或数据库密码。

GPU publish 请求中的敏感账号凭证只存在于 AES-GCM 短时任务信封内，服务端不记录请求头/请求体；响应只含 `publish_id`、远端状态、TikTok log ID和安全错误。

## 错误码

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` | 字段或时间格式错误 |
| 400 | `invalid_caption_template` | 描述模板为空、含 NUL 或结构无效 |
| 400 | `caption_content_id_required` | 描述模板缺少 Drama ID 占位符，或旧 `caption_text` 未保留准确 Drama ID |
| 400 | `caption_placeholder_invalid` | 描述模板包含未知占位符 |
| 400 | `caption_length_invalid` | 按真实 Drama ID 渲染后超过 2200 个 UTF-16 单位 |
| 400 | `tt_caption_template_render_mismatch` | 同时提交的模板和最终描述不一致 |
| 400 | `tt_post_consent_required` | 未完成显式发布同意 |
| 403 | `permission_denied` | 无 TT 发布池权限 |
| 404 | `tt_account_not_found` | 账号不存在或不满足候选条件 |
| 409 | `tt_post_material_already_used` | 素材已有排期或发布历史 |
| 409 | `tt_post_account_time_conflict` | 同账号同一时间已有任务 |
| 409 | `tt_creator_info_changed` | 账号实时能力与冻结快照不同 |
| 409 | `tt_post_unknown_no_retry` | 结果不明，禁止自动重发 |
| 409 | `tt_post_reconcile_only` | 已有 `publish_id`，只能 reconcile |
| 502 | `tt_upstream_rejected` | TikTok 返回已脱敏错误 |
| 503 | `tt_post_service_unavailable` | CPU sidecar 或其依赖暂不可用 |

## 兼容性说明

- 用户原文变量为 `{{contect_id}}`，模板渲染兼容该拼写；数据库和 API 始终使用正确字段名 `content_id`。
- 当前默认模板仍为上述产品文案，但新建任务允许编辑前后文；`caption_template` 与按素材真实 `content_id` 渲染的 `caption` 分别冻结。
- Core 和 Service 的幂等比较都包含模板和最终文案；相同幂等键改变素材、账号、时间、模板、渲染文案或其他冻结设置时返回 `tt_post_idempotency_conflict`。
- 历史固定描述和历史自定义描述均不做破坏性改写；旧 `caption_text` 请求和缺省默认模板请求可按原幂等键精确重放。
- 页面批量功能复用现有单项路由和现有三张 TT SQLite 表，不增加批量接口、批次表或 MySQL 变更。
- 批量部分失败不是事务性整批回滚：每个 preview/queue 请求独立，已成功项保留，失败项安全汇总，后续项继续。
- 同账号同一 UTC 发布时间唯一约束保持不变；页面通过首条时间加正整数间隔避免本批内部重时点，服务端仍以数据库约束处理并发和历史冲突。
- 时间输入为 `Asia/Shanghai`，数据库统一存 UTC。
- 本功能完全独立于 `x_post_*` 表和 X 发布状态。
- 三重门禁默认关闭；关闭态 API 仍支持账号、素材、成片、队列和对账演练，但不调用 TikTok Direct Post init。
