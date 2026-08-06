# API 文档

## 接口列表

- `GET /api/public/tt-drama/featured-by-language/<lang>.json`：新页面使用的单语言榜单。
- `GET /api/public/tt-drama/featured-by-language`：保留的全语言 v2 兼容接口。
- `GET /tt-featured-covers/<sha256>.webp`：内容寻址的同源 Featured 缩略图。

## 请求/响应

`<lang>` 只允许规范化的小写语言标签，例如 `en`、`pt-br`、`zh-tw`。

成功响应：

```json
{
  "schema_version": 3,
  "source_date": "2026-08-05",
  "generated_at": "2026-08-06T18:00:00+08:00",
  "language": "en",
  "items": [
    {
      "content_id": "zALq8tHA9a",
      "title": "How to Ride a Billionaire Cowboy",
      "cover_url": "https://static-v1.mydramawave.com/original-cover.jpg",
      "thumbnail_url": "/tt-featured-covers/<sha256>.webp",
      "language": "en",
      "episode_count": 0
    }
  ]
}
```

响应必须包含恰好五条，不包含 spend、内部数据库字段或归因记录。
`cover_url` 始终保留受白名单约束的原始 HTTPS 封面；`thumbnail_url`
是可选值，生成失败时为空字符串，页面会回退到 `cover_url`。

## 错误码

- `404`：语言文件或 hash 封面不存在、路径不符合严格正则。
- 静态接口不返回业务 500 JSON；生成失败时继续保留上一份有效文件。

## 兼容性说明

- 原全语言 endpoint、resolver 和 W2A URL 合同不变。
- 新页面优先单语言 v3；加载失败时使用 HTML/JS 内置占位，不构造未验证跳转。
- HTML no-store；locale JSON 缓存 5 分钟；hash WebP/JS immutable。
