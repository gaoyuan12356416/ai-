# X Post 每日发布与日志 API

## 鉴权边界

- Sidecar `/internal/*`：仅 loopback + `X_INTERNAL_TOKEN`。
- AI 后台 `/api/admin/x-posts/*`：仅 Feishu Cookie 管理员；API Token 和普通用户拒绝。
- 所有日志响应 `Cache-Control: no-store`，不返回 OAuth/数据库敏感值。

## POST /internal/posts/queue/{queue_id}/publish

请求体为 `{}`。账号、候选、page、URL 参数全部从已冻结 queue 读取，调用方不得覆盖。

成功响应安全字段：

```json
{
  "item": {
    "status": "published",
    "log_id": 2,
    "short_url": "https://ai.yingliangads.com/s2l/2.html",
    "post_id": "1234567890",
    "preview_url": "https://x.com/example/status/1234567890"
  }
}
```

## POST /internal/posts/logs/query

请求字段：`actor`、`scope=all`、`page`、`page_size<=100`，可选 `run_date/account_id/status/material_id`。

返回字段仅包含 run/queue/log ID、账号公开标识、素材/剧公开元数据、合规快照、状态、尝试次数、unknown、短链、X 预览、脱敏错误码/错误说明和时间。

## POST /internal/posts/runs/query

返回每日批次的 `run_date/source_date/status/expected/queued/published/failed/unknown/started_at/finished_at` 与分页信息。

## GET /api/admin/x-posts/logs

查询参数与 Sidecar 日志查询白名单一致。主后台从 Cookie session 构造 actor，不接受浏览器传入 owner/admin 身份。

## GET /api/admin/x-posts/runs

查询每日批次列表，管理员只读。

## 稳定错误码

- `x_post_material_already_used`
- `x_post_account_day_already_reserved`
- `x_post_daily_run_exists`
- `x_post_daily_candidate_shortage`
- `x_post_retry_requires_review`
- `x_post_unknown_outcome`
- `x_post_rate_limited`
- `x_posts_unavailable`
