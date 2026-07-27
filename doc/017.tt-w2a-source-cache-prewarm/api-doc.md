# API 文档

## 接口兼容结论

本需求不新增公开接口。以下现有接口路径和前端调用方式保持不变：

- `GET /api/public/tt-drama/resolve`
- `GET /api/public/tt-drama/featured`

变化仅在服务端资源来源：`app.py` 在 `TT_DRAMA_RESOURCE_SOURCE=w2a_cache` 时选择共享资源服务，标题、描述和封面由 W2A 原始 HTML 与本地 SQLite 缓存提供。

## Resolve

```http
GET /api/public/tt-drama/resolve?content_id=Ag0rfr5F0F
Accept: application/json
```

参数：

| 参数 | 必填 | 规则 |
| --- | --- | --- |
| `content_id` | 是 | 只出现一次，`[A-Za-z0-9_-]{10,32}`，大小写敏感 |

成功响应：

```json
{
  "found": true,
  "data": {
    "content_id": "Ag0rfr5F0F",
    "title": "Her Beast",
    "description": "Betrayed by her fiancé and sister on her wedding day...",
    "cover_url": "https://cdn.usrgrow.com/storage/icons/example_banner.jpg",
    "country": "",
    "language": "",
    "episode_count": 0,
    "source_updated_at": "2026-07-27T12:00:00+00:00"
  }
}
```

未找到：

```json
{
  "found": false,
  "error": "not_found",
  "message": "No matching DramaWave story was found."
}
```

响应头保持：

- `Cache-Control: no-store`
- `X-TT-Drama-Cache: BYPASS | MISS | HIT | NEGATIVE_HIT | STALE | RATE_LIMITED | OVERLOADED | ERROR`
- `Server-Timing: tt-drama-resolver;dur=<milliseconds>`

缓存状态语义：

- `MISS`：公开兼容语义；内部 SQLite 未命中后发生 `ORIGIN_FILL` 或 `NEGATIVE_FILL` 时都映射为该值。
- `HIT`：公开兼容语义；内部 `DISK_HIT` 映射为该值。SQLite 是唯一数据缓存，没有第二份进程内数据缓存。
- `NEGATIVE_HIT`：明确 404/实际 ID 不匹配的短期负缓存命中。
- `STALE`：源暂不可用时返回仍在兜底期的旧正缓存。
- `BYPASS`、`RATE_LIMITED`、`OVERLOADED`、`ERROR`：沿用现有路由层语义。

`ORIGIN_FILL`、`DISK_HIT` 和 `NEGATIVE_FILL` 仅用于内部可观测性，不直接出现在公开响应头，避免扩大旧客户端依赖的状态枚举。

错误码：

| HTTP | error | 场景 |
| --- | --- | --- |
| 400 | `invalid_request` | 参数缺失、重复、多余或格式错误 |
| 404 | `not_found` | W2A 明确 404，或源码实际 ID 与请求 ID 不一致 |
| 429 | `rate_limited` | 客户端请求过于频繁 |
| 503 | `resolver_unavailable` | 超时、429/5xx、HTML 超限、解析失败、数据盘/SQLite 不可用 |
| 503 | `resolver_overloaded` | 服务端在途请求或回源并发达到上限 |

兼容和安全约束：

- 客户端不能传入源 URL、host、path、重定向目标或封面域名。
- 源客户端不跟随重定向，也不自动重试。
- 接口不接收 `af_adset_id` 等投放参数；这些参数仍由 `/tt` 页面在 resolver 成功后拼入固定 W2A 跳转链接。
- `.info .desc` 元素必须存在，但其文本允许为空字符串。
- 不持久化或对外返回完整源 HTML、临时源 URL、完整深链、Pixel、SDK、OneLink、花费、租约或内部错误堆栈。
- 封面 URL 由用户浏览器直接访问允许的 HTTPS CDN；resolver 不下载图片。

## Featured

请求与响应结构保持现状：

```http
GET /api/public/tt-drama/featured
Accept: application/json
```

```json
{
  "schema_version": 1,
  "source_date": "2026-07-26",
  "generated_at": "2026-07-27T18:00:03+08:00",
  "items": [
    {
      "content_id": "Ag0rfr5F0F",
      "title": "Her Beast",
      "cover_url": "https://cdn.usrgrow.com/storage/icons/example_banner.jpg",
      "language": "",
      "episode_count": 0
    }
  ]
}
```

- 仍要求 `items` 恰好 5 条、Content ID 唯一。
- 排名仍来自昨日 W2A 花费；MySQL 不再查询剧库元数据，标题和封面来自共享 SQLite/W2A 服务。
- 刷新失败不覆盖 last-known-good 文件。
- 不返回 spend 或任何内部数据源信息。

## 生产接口验收

- `Ag0rfr5F0F` 返回 `Her Beast` 与 CDN 封面；最终复核缓存状态为 `HIT`。
- `ZZZZZZZZZZ` 首次返回 `404`、`X-TT-Drama-Cache: MISS`；再次及最终复核返回 `404`、`X-TT-Drama-Cache: NEGATIVE_HIT`。
- 短 ID 返回 `400`、`X-TT-Drama-Cache: BYPASS`。
- 连续 30 次 `HIT`：p50 `13.358 ms`、p95 `14.162 ms`、max `15.401 ms`。
- Featured 的 `source_date` 为 `2026-07-26`，返回 5 项且均可跳转。
- 真实浏览器中封面、标题和简介正常展示，错误 ID 无卡片，`af_adset_id=XXX` 正确透传。
- 错误 ID 在浏览器 console 中只有预期接口 `404 Failed to load resource`，没有 JavaScript exception 或 CSP warning/error。
