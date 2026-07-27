# API 文档

## 接口列表

- `GET /api/public/tt-drama/featured`
- 公开、同源、无 Cookie/Token。
- Nginx 直接读取本地最后成功 JSON，不进入 Python API，不查询数据库。

## 请求/响应

请求无业务参数：

```http
GET https://ai.yingliangads.com/api/public/tt-drama/featured
Accept: application/json
```

成功：

```json
{
  "schema_version": 1,
  "source_date": "2026-07-26",
  "generated_at": "2026-07-27T18:00:03+08:00",
  "items": [
    {
      "content_id": "l9rP6ey2CB",
      "title": "Example Drama",
      "cover_url": "https://static-v1.mydramawave.com/example.jpg",
      "language": "en",
      "episode_count": 80
    }
  ]
}
```

固定响应约束：

- `items` 恰好 5 条且 `content_id` 唯一。
- 不返回 `spend`、素材 ID、平台、广告实体、数据库信息或内部路径。
- 最大文件 32 KiB。
- `Cache-Control: public, max-age=300, stale-while-revalidate=3600`。
- `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`。
- ETag/Last-Modified 由 Nginx 静态文件能力提供。

## 错误码

| HTTP | 场景 | 页面处理 |
| --- | --- | --- |
| 404 | 首次快照尚未生成或文件被回滚 | 保留人工审核静态卡片 |
| 403/405 | 非 GET/HEAD 方法 | 不重试 |
| 5xx/超时 | Nginx/网络暂不可用 | 2 秒内回退静态卡片 |
| 200 但 JSON 非法/不足 5 条 | 缓存契约损坏 | 拒绝动态内容并回退 |

刷新失败不会产生 API 错误覆盖：生成器保留上一份可读
last-known-good，因此数据库暂时不可用时静态 API 仍可返回 200。

## 兼容性说明

- 当前搜索接口 `/api/public/tt-drama/resolve` 保持不变且继续 `no-store`。
- 卡片目标复用现有 W2A 生成器，仍固定
  `af_dp=<content_id>&c=TTpost&af_c_id=0001`。
- 页面入口上的合法非保留查询参数按原顺序透传；大小写变体核心键仍被拦截。
- `source_date` 不是最终财务结算声明，而是缓存生成时的昨日投放快照。
- 浏览器拒绝 `generated_at` 超过 72 小时/未来超过 24 小时，以及
  `source_date` 晚于上海昨日或落后超过 72 小时的动态快照，并回退人工
  静态卡片。
