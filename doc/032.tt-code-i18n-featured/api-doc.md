# API 文档

## GET /api/public/tt-drama/featured-by-language

读取数据盘上的分语言 Featured last-known-good 快照。该路由由 Nginx exact alias 直接提供，不进入应用进程，不查询 MySQL 或 Redis。

### 请求

- 方法：`GET`
- 鉴权：无
- Query：无。浏览器语言只在前端用于选择响应中的榜单桶。
- 缓存：`public, max-age=300, stale-while-revalidate=3600`

### 200 响应

以下为单条 item 结构节选；实际响应中的每个语言桶固定包含 5 条。

```json
{
  "schema_version": 2,
  "source_date": "2026-08-04",
  "generated_at": "2026-08-05T15:30:00+08:00",
  "default_language": "en",
  "rankings": {
    "en": [
      {
        "content_id": "l9rP6ey2CB",
        "title": "Example drama",
        "cover_url": "https://cdn.usrgrow.com/example.jpg",
        "language": "en",
        "episode_count": 60
      }
    ]
  }
}
```

### 合同

- 顶层仅允许 `schema_version/source_date/generated_at/default_language/rankings`。
- `schema_version` 固定为 2；`default_language` 固定为 `en`。
- `rankings` 最多 32 个规范语言键，必须包含 `en`。
- 每个语言桶恰好 5 条；同一 `content_id` 不能跨桶重复。
- item 仅允许 `content_id/title/cover_url/language/episode_count`，其中 `language` 必须等于桶键。
- 任意层禁止 `spend` 和 `spend_n`；响应最大 256 KiB。
- 文件刷新使用临时文件、fsync 和原子 replace；生成失败时继续返回上次完整文件。

### 错误

- 文件不存在或 Nginx 无权读取时返回 404/403。页面会以当前 UI 语言显示 5 条不可点击的本地占位卡（未支持语言使用英文），不会尝试不安全跳转。

## 兼容接口

`GET /api/public/tt-drama/featured` 保持原 v1 schema 和原 `current.json` 不变，继续服务旧 `/tt`。

`GET /api/public/tt-code/resolve` 保持原参数和响应合同；本需求不向该接口或最终 W2A URL 增加语言参数。
