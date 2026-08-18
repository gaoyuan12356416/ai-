# API 文档

## GET `/api/admin/x-accounts`

既有 Cookie 管理员路径、鉴权、Sidecar账号字段和 no-store 保持。主 API 从本地 cache 新增：

```json
{
  "items": [{"id": 7, "operating_stats": {
    "published_posts_total": 12,
    "published_posts_yesterday": 2,
    "reposts_total": 3,
    "reposts_yesterday": 1,
    "revenue_total_usd": "123.450000",
    "revenue_yesterday_usd": "4.560000"
  }}],
  "operating_stats_meta": {
    "status": "fresh",
    "available": true,
    "stale": false,
    "generated_at_utc": "2026-08-18T01:10:00Z",
    "unallocated_revenue": {"total_usd": "7.000000", "yesterday_usd": "1.000000"}
  }
}
```

cache missing 时仍为 200、`status=missing`、六项 null；有效缓存中无历史账号六项为 0；stale 保留旧值并标记。无新增 HTTP 路由。
