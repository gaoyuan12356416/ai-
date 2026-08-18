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
    "stale_reasons": [],
    "generated_at_utc": "2026-08-18T01:10:00Z",
    "business_date": "2026-08-18",
    "yesterday_date": "2026-08-17",
    "unallocated_revenue": {"total_usd": "7.000000", "yesterday_usd": "1.000000"}
  }
}
```

cache missing 时仍为 200、`status=missing`、六项 null；有效缓存中无历史账号六项为 0。TTL 超限、跨北京业务日或生成时间超前超过 5 分钟时 stale，保留旧值并返回原因；页面直接显示 snapshot 的 `yesterday_date`。无新增 HTTP 路由。
