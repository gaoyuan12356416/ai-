# API 文档

## 接口列表

- `GET /api/public/tt-drama/resolve`

## 请求/响应

请求参数：

| 参数 | 必填 | 规则 | 说明 |
| --- | --- | --- | --- |
| `content_id` | 是 | `[A-Za-z0-9_-]{10,32}`，且只能出现一次 | DramaWave 内容 ID |

成功响应：

```json
{
  "found": true,
  "data": {
    "content_id": "l9rP6ey2CB",
    "title": "Drama title",
    "description": "Drama description",
    "cover_url": "https://allowed.example/cover.jpg",
    "country": "jp",
    "language": "ja",
    "episode_count": 150,
    "source_updated_at": "2026-07-27T00:00:00Z"
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

观测响应头：

- `X-TT-Drama-Cache: BYPASS | MISS | HIT | NEGATIVE_HIT | STALE | RATE_LIMITED | OVERLOADED | ERROR`
- `Server-Timing: tt-drama-resolver;dur=<milliseconds>`
- `Cache-Control: no-store`

## 错误码

| HTTP | error | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` | 参数缺失、重复、多余或格式错误 |
| 404 | `not_found` | 没有匹配的可用剧集 |
| 429 | `rate_limited` | 当前客户端请求过于频繁 |
| 503 | `resolver_unavailable` | 只读剧库未配置、超时或暂不可用 |
| 503 | `resolver_overloaded` | 全局 resolver 在途请求已达到上限 |

## 兼容性说明

- 接口同源、无需登录，不支持跨域。
- 服务端固定 DramaWave `app_id=1479` 和资源表；客户端不能指定数据库、产品或跳转目标。
- 接口不接收或返回 `af_adset_id` 等投放参数；这些参数继续由 `/tt` 前端在确认命中后拼入固定 W2A URL。
- 404 只代表只读剧库精确查询无可用记录；依赖故障必须返回 503。
