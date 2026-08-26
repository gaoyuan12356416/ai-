# API 文档

所有浏览器 API 都要求现有 `drama_synthesis` 模块权限。GPU internal API 使用现有 bearer token。响应只包含安全字段。

## 新接口

### `GET /api/drama-material/random-template-catalog`

返回 `{item:{version,profile,manifest_sha256,categories}}`。categories 只含安全的 `name,sha256,media_type,size`，不含服务器绝对路径。

### `GET /api/gpu-video/random-overlay/catalog`

HK internal catalog。要求 `Authorization: Bearer <GPU_VIDEO_WORKER_TOKEN>`；token 或素材配置缺失时失败关闭。

### `POST /api/gpu-video/render` 扩展

新字段：

```json
{
  "outputs": {"random_template_video": true},
  "random_template_recipe": {"version": 1, "profile": "drama-random-overlay-h264-720x1280-v1"}
}
```

完整 recipe 由 CPU 冻结后传送。响应新增 `random_template_video_url`、`random_template_output_sha256`、`random_template_output_profile`、`random_template_recipe_sha256`。

### `POST /api/drama-material/jobs`

`outputs` 新增 `random_template_video`；选中时 `random_template` 必填：

```json
{"mode":"auto","layers":{}}
```

或手动 `layers` 精确包含 `border,opacity_video,corners,tint`。四输出全 false 返回 400。新 UI 不再发送 `advanced_options.cover_template` 和 `advanced_options.naming_rule`；服务端旧默认保持。

### `POST /api/drama-material/jobs/<job_id>/short-link`

只允许完成任务。幂等返回安全字段 `id,short_url,long_url,publish_state,published_at_utc,reused`。publisher 未配置返回 503。

### `GET /api/drama-material/jobs/<job_id>/youtube-channels`

只允许完成任务。只返回当前 app 的 eligible channel：`channel_local_id,channel_id,channel_name,youtube_account_id,upload_eligible,comment_eligible`。

### `POST /api/drama-material/jobs/<job_id>/youtube-publish`

请求：

```json
{
  "operation_id":"yt:<uuid>",
  "channel_local_id":"...",
  "channel_id":"UC...",
  "youtube_account_id":"...",
  "source_kind":"random_template_video",
  "title":"...",
  "description":"...",
  "comment":"...",
  "duplicate_confirmed":false
}
```

`source_url` 不接受浏览器输入，服务端从已完成 job 解析。返回 202 和分离的 `status,video_state,comment_state`。不返回 access token、refresh token、client config、resumable URI 或本地路径。

## 关键错误码

| code | HTTP | 含义 |
| --- | --- | --- |
| `drama_job_not_completed` | 409 | 任务未完成 |
| `drama_template_catalog_unavailable` | 503 | GPU 目录不可用 |
| `drama_recipe_result_mismatch` | 502 | GPU 回传配方身份不符 |
| `drama_short_link_publisher_not_configured` | 503 | 无短链发布适配器 |
| `youtube_channel_not_eligible` | 409 | 频道/授权不符合条件 |
| `youtube_comment_scope_missing` | 409 | 评论缺少 force-ssl |
| `youtube_duplicate_confirmation_required` | 409 | 已有成功，需二次确认 |
| `youtube_previous_outcome_unknown` | 409 | 已有未知结果，禁止替代发布 |

## 兼容性

原 jobs/products/retry/delete、旧 outputs 和历史 advanced JSON 保持可读。历史空 outputs 按原三输出默认解释，随机模板默认 false。
