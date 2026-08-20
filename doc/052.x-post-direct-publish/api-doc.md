# API 文档

## 接口列表

- `POST /internal/posts/schedule-plan`
- `POST /internal/posts/queue/{queue_id}/publish`
- `POST /internal/posts/storage/preflight`

## 请求/响应

schedule-plan 的内部 candidate 新增：

```json
{
  "media_validation_mode": "deferred",
  "preflight_sha256": "",
  "preflight_size": 0,
  "preflight_duration": 0
}
```

`preflight_duration` 在 deferred 下只是可选路由提示：素材使用源库时长；未知时长短剧 direct 为 0、预冻 Relay 为 141。发布结果仍沿用现有安全 DTO，不暴露 Token 或内部路径。

## 错误码

- `invalid_request`：非 schedule 路径请求 deferred、模式/指纹组合不合法。
- `x_post_premium_relay_unavailable`：长素材或保守 Relay 短剧没有同语言 Premium 源账号。
- `media_preflight_changed`：仅旧 `preflight` 队列的指纹/时长漂移。
- 下载/probe/X API 既有明确错误：写 failed 并继续下一条。
- `x_post_rate_limited` / unknown outcome：停止批次。

## 兼容性说明

- 新列默认 `preflight`，所有历史队列和非 schedule 路径行为不变。
- deferred 仅 scheduler 内部 bearer 可创建；管理员前端与公开 API 无需修改。
- 旧发布代码不能处理 deferred queue，回滚需遵守 deploy 文档的清账门禁。
