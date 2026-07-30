# API 文档

## 接口列表

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/admin/tt-posts/accounts` | 安全账号列表和数据库候选状态 | `ttPostPool` |
| POST | `/api/admin/tt-posts/creator-info` | GPU 实时账号能力预检 | `ttPostPool` + 同源 |
| POST | `/api/admin/tt-posts/materials/preview` | 单个素材、Drama ID和 GPU 成片预览；页面批量时逐条调用 | `ttPostPool` + 同源 |
| GET | `/api/admin/tt-posts/material-pool` | 查询账号 FIFO 待发布素材 | `ttPostPool` |
| POST | `/api/admin/tt-posts/material-pool` | 将一个已核对素材冻结到账号发布池 | `ttPostPool` + 同源 |
| GET | `/api/admin/tt-posts/schedule` | 查询账号每日上海时间、版本和下一次执行 | `ttPostPool` |
| POST | `/api/admin/tt-posts/schedule` | 以乐观版本保存账号每日时间 | `ttPostPool` + 同源 |
| POST | `/api/admin/tt-posts/run-now` | 幂等领取下一条并立即唤醒同一 runner | `ttPostPool` + 同源 |
| GET | `/api/admin/tt-posts/queue` | 查询发布任务 | `ttPostPool` |
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

## 页面批量入池合同

不新增批次 API 或批次表。页面完成以下编排：

1. textarea 接受 1–100 个正整数素材 ID，支持空白、换行、中英文逗号和中英文分号分隔。
2. 页面按首次出现顺序规范化、去重；任一 token 非法时，在调用后台前整体拒绝。
3. 页面对每个唯一 ID 依次调用现有 `POST /materials/preview`。单项失败写入页面失败明细，继续下一项。
4. 页面对每个成功项依次调用 `POST /material-pool`；每项带稳定幂等键、目标账号、真实 Drama ID、编辑模板和同一批确认时间。
5. 服务端重新解析素材并复用确定性 GPU job，冻结真实 Drama ID、成片和最终文案；客户端预览字段只用于身份核对。
6. 单项失败不回滚此前成功项，也不阻断随后项。页面最终展示预览成功/失败、入池成功/失败数量和逐项结果。

因此 HTTP 层仍只返回单项 `item`；所谓“批量结果”是浏览器对多次单项响应的安全汇总。

## 请求/响应

单个素材预览：

```json
{
  "material_id": "5824343"
}
```

预览与随后入池基于素材 ID、真实 `content_id`、源地址/指纹、裁剪秒数、媒体 profile、当前 Logo SHA 和固定片尾 SHA 生成同一确定性 prepare job 身份。默认 profile 为 `tt-post-hevc-720x1280-v2`，兼容回退 profile 为 `tt-post-h264-720x1280-v2`；两者及任何旧 profile 的产物都不得跨 profile 作为 ready 缓存复用。CPU prepare 请求强制携带 `expected_profile`，GPU 在下载与制作前完成握手；GPU 返回后 CPU 再复验响应 profile。ready 复用时 GPU 重新读取并哈希当前 Logo/片尾；源、profile 或品牌资产身份任一变化时不得复用旧完成产物。

冻结一条账号池素材：

```json
{
  "idempotency_key": "tt-post-pool:019fa...:5824343",
  "material_id": "5824343",
  "content_id": "Y9v1yQcFqM",
  "source_account_id": "700",
  "caption_template": "Watch the full story in the app 🎬\n\nDrama ID: {{contect_id}}\n\nVisit my profile → Open the link → Search the Drama ID → Watch now.",
  "consent": {
    "accepted": true,
    "version": "tt-recurring-post-consent-20260730",
    "accepted_at": "2026-07-30T07:00:00.000Z"
  }
}
```

`caption_template` 是当前页面的可编辑模板。它必须至少包含
`{{contect_id}}` 或 `{{content_id}}`，且不得包含其他未知占位符。服务端
重新解析素材真实 `content_id` 后渲染并冻结 `caption_template` 与最终
`caption`；最终描述不得超过 2200 个 UTF-16 单位。请求中的 `content_id`
仅用于核对页面预览身份，不能覆盖服务端真实映射。

隐私、互动、商业披露和 AIGC 不由发布池请求决定。服务端必须读取
“TT 个号管理”中已保存且经 `creator_info` 校验的账号设置；未配置时返回
`tt_account_settings_required`。旧页面即使继续提交这些字段，服务端也不得用它们覆盖账号级设置。

保存每日时间：

```json
{
  "source_account_id": "700",
  "enabled": true,
  "publish_times": ["11:00"],
  "timezone": "Asia/Shanghai",
  "expected_version": 3,
  "consent": {
    "accepted": true,
    "version": "tt-recurring-post-consent-20260730",
    "accepted_at": "2026-07-30T07:00:00.000Z"
  }
}
```

手动额外发布一条：

```json
{
  "source_account_id": "700",
  "idempotency_key": "tt-post-manual:019fa..."
}
```

相同手动幂等键重放只返回同一个 run/queue；新幂等键代表一次新的人工明确动作。手动 run 不修改或替代账号当天自动时点。

兼容旧调用方：

- 只提交旧字段 `caption_text`：按已经渲染的单素材描述处理，并要求其中的 Drama ID 与真实 `content_id` 一致。
- `caption_template`、`caption_text` 均省略：使用当前默认模板。
- 两字段同时提交：`caption_text` 必须与 `caption_template` 按真实 Drama ID 渲染的结果逐字一致。
- 旧任务以原始幂等键和原始请求精确重放：直接返回既有冻结任务，不修改历史模板/描述，不再次调用 GPU。

入池成功响应只返回安全字段：

```json
{
  "item": {
    "id": 31,
    "material_id": "5824343",
    "content_id": "Y9v1yQcFqM",
    "account_id": "700",
    "status": "available",
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

## 内部调度合同

- `POST /internal/tt-posts/schedules/due` 只允许 loopback bearer。
- runner 每分钟先调用 due，再调用既有 claim/publish/reconcile。
- due 先恢复 `claimed` 且未绑定 queue 的 run，再扫描最近 600 秒的到期时点；相同自动 run key 只返回原任务，超过窗口不创建新任务。
- 自动 run key 为 `tt-post:auto:v1:{account_id}:{YYYY-MM-DD}:{HHMM}`。
- 手动 run key 由服务端从账号和客户端幂等键确定，不把请求键暴露到日志。
- 自动 tick、手动双击、服务重启和 path/timer 同时唤醒都依赖 SQLite 唯一键返回原 run。
- 同一 run 额外使用 120 秒 execution lease 与接管即更新的 fencing token；`freeze/release/bind` 均在事务内校验当前 owner，过期执行者不能冻结队列、释放素材或完成绑定。
- 三重门禁关闭、账号有 active/unknown queue、空池或 creator 预检失败时不消费素材、不调用 TikTok init。

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
| 404 | `material_not_found` | 素材 ID 不存在 |
| 409 | `material_type_not_video` | 素材不是视频 |
| 409 | `material_deleted` | 素材已删除 |
| 409 | `material_duration_out_of_range` | TT 源素材时长不在 0–3600 秒安全范围 |
| 409 | `tt_post_material_already_used` | 素材已有排期或发布历史 |
| 409 | `tt_post_account_time_conflict` | 同账号同一时间已有任务 |
| 409 | `tt_account_settings_required` | 账号尚未在 TT 个号管理中完成发布设置 |
| 409 | `tt_creator_info_changed` | 账号实时能力与冻结快照不同 |
| 409 | `tt_post_unknown_no_retry` | 结果不明，禁止自动重发 |
| 409 | `tt_post_reconcile_only` | 已有 `publish_id`，只能 reconcile |
| 409 | `tt_post_schedule_version_conflict` | 每日排期已被其他操作修改 |
| 409 | `tt_post_recurring_pool_empty` | 当前账号没有待发布素材 |
| 409 | `tt_post_account_publish_busy` | 当前账号已有执行中或待核对任务 |
| 409 | `tt_post_live_gates_closed` | 发布门禁未全部开放，未消费素材 |
| 409 | `prepare_profile_mismatch` | prepare 请求的 `expected_profile` 与 GPU 当前 profile 不一致，已在下载前拒绝 |
| 409 | `tt_prepared_media_profile_mismatch` | GPU 返回的成片 profile 与 CPU 当前预期不一致 |
| 409 | `prepare_idempotency_conflict` | 同一 prepare job 的源、profile、Logo 或固定片尾身份发生变化 |
| 500 | `prepared_media_invalid` | GPU 最终成片为空或超过 4 GiB 合同 |
| 502 | `tt_upstream_rejected` | TikTok 返回已脱敏错误 |
| 503 | `tt_post_service_unavailable` | CPU sidecar 或其依赖暂不可用 |

4 GiB 是 API 的硬安全上限，不代表交付合格。当前 34.8 分钟素材的交付验收要求低于 500 MB；默认 HEVC 方案按 60 秒样片预计约 295 MB，H.264 兼容回退预计约 433 MB。两者都不是完整生产实测，实际结果须以新 profile 生产重跑为准。

## 兼容性说明

- 用户原文变量为 `{{contect_id}}`，模板渲染兼容该拼写；数据库和 API 始终使用正确字段名 `content_id`。
- 当前默认模板仍为上述产品文案，但新建任务允许编辑前后文；`caption_template` 与按素材真实 `content_id` 渲染的 `caption` 分别冻结。
- Core 和 Service 的幂等比较都包含模板和最终文案；相同幂等键改变请求中的素材、账号、时间、模板、渲染文案或确认信息时返回 `tt_post_idempotency_conflict`。
- 历史固定描述和历史自定义描述均不做破坏性改写；旧 `caption_text` 请求和缺省默认模板请求可按原幂等键精确重放，即使原发布时间已经临近或经过也先返回历史任务。之后修改账号级设置不改变既有任务。
- 页面批量功能复用单项 preview，并新增单项 material-pool；不增加批次表或 MySQL 变更。
- SQLite 从旧四表以只增方式扩为七表；旧 queue 不重建、不改写，回滚保留新账本。
- 批量部分失败不是事务性整批回滚：每个 preview/material-pool 请求独立，已成功项保留，失败项安全汇总，后续项继续。
- 每日时间输入为 `Asia/Shanghai` 严格 `HH:MM`，run 和 queue 统一存 UTC。
- TT 解析器使用独立 3600 秒安全上限；X selector 仍严格保持 140 秒。
- 本功能完全独立于 `x_post_*` 表和 X 发布状态。
- 三重门禁默认关闭；关闭态 API 仍支持账号、素材、成片、每日排期保存以及队列/事件只读查询，但自动和手动执行均不消费素材、不创建新队列，也不调用 TikTok Direct Post init。旧的精确 `POST /api/admin/tt-posts/queue` 已从主后台公开代理移除。
