# API 文档

## 状态

接口已于 2026-08-04 部署到生产，公开入口为 `https://ai.yingliangads.com/tt-code`。文中的示例 payload 仍是占位数据，不是生产凭据；实际线上验证摘要见 `test-report.md`。

## 公共组合接口

```http
GET /api/public/tt-code/resolve?query=<code-or-content_id>&source=Search|Featured
```

Nginx 将该 exact route 转发到主 app `127.0.0.1:8787`。主 app 负责公开输入校验、既有 token bucket、in-flight gate、目标校验和 DramaWave 剧目存在性/元数据校验，并以现有内部 bearer 调用 sidecar 的私有 resolver。前端一次请求即可得到 route 与剧目元数据，不再串行调用 `/api/public/tt-drama/resolve`。

### 请求参数

| 参数 | 必填 | 规则 |
| --- | --- | --- |
| `query` | 是 | 四位 ASCII 字母数字按 code 处理并转大写；否则必须是现有 resolver 接受的 10..32 位 `[A-Za-z0-9_-]` content ID |
| `source` | 是 | 区分大小写，只接受 `Search` 或 `Featured` |

只接受这两个参数且各出现一次。搜索框发送 `Search`，Featured 卡片发送 `Featured`。code exact 的冻结 channel 始终为 `TT`，不受 `source` 覆盖。

### 成功响应

```json
{
  "found": true,
  "item": {
    "content_id": "LZ4b4w5k3h",
    "title": "Example Drama",
    "description": "Example description",
    "cover_url": "https://cdn.example/cover.jpg",
    "country": "US",
    "language": "en",
    "episode_count": 60,
    "source_updated_at": "2026-08-04T00:00:00Z",
    "target_url": "https://www.dramawavew2a.com/ads/101/2250/view?...",
    "query_type": "code",
    "route_mode": "code_exact",
    "code": "AB12"
  }
}
```

`code` 只在 `query_type=code` 时返回。content ID 的 `published_clone` 虽然来自某条 code route，主 app 会移除该 code，避免把非查询主键暴露给前端。

| 字段 | 取值 |
| --- | --- |
| `query_type` | `code` 或 `content_id` |
| `route_mode` | `code_exact`、`published_clone` 或 `generic_fallback` |

响应始终 `Cache-Control: no-store`，并使用现有 `X-TT-Drama-Cache` 和 `Server-Timing`。当前实现没有 `X-TT-Code-Cache` 响应头。

## 路由语义

### code exact

```http
GET /api/public/tt-code/resolve?query=ab12&source=Search
```

- 输入转为 `AB12`，只按 `tt_post_code_route.code` 主键查询，不按 `state` 过滤。
- 返回该队列冻结的完整 URL，`af_channel=TT`。
- 主 app 再用 route 中的 `content_id` 做现有 DramaWave 校验；剧已不存在时公共响应 404，不输出 CTA。
- 正式冻结 URL 参数顺序为：

```text
af_dp,c,af_adset,af_adset_id,af_ad,af_ad_id,af_channel,af_c_id
```

### content ID / Featured published clone

```http
GET /api/public/tt-code/resolve?query=LZ4b4w5k3h&source=Search
GET /api/public/tt-code/resolve?query=LZ4b4w5k3h&source=Featured
```

在同剧 `state='published'` 的 route 中按以下顺序只取一条：

```text
published_at DESC, created_at DESC, queue_id DESC
```

重建 URL 时保留所有冻结归因值，只把 `af_channel` 改为请求的 `Search` 或 `Featured`；不更新数据库、不生成 code。

### generic fallback

剧存在但从未有 published route 时返回：

```text
https://www.dramawavew2a.com/ads/101/2250/view?af_dp=<content_id>&c=TTpost&af_c_id=0001&af_channel=Search|Featured
```

fallback 不伪造 page、素材或 queue 参数。若 content ID 本身不存在，虽然 sidecar 可构造 fallback，主 app 的剧目校验仍会把公共结果收敛为 404。

## 目标 URL 校验

主 app 在返回前验证：

- scheme 为 `https`；host 精确 `www.dramawavew2a.com`；path 精确 `/ads/101/2250/view`
- 无 username/password、自定义端口、fragment 或重复/空参数
- `af_dp` 与被验证的 `content_id` 一致
- code exact 的完整参数集合和 `af_channel=TT`
- published clone 的完整参数集合和 `af_channel=source`
- generic fallback 只有 `af_dp,c,af_c_id,af_channel`，且 `c=TTpost`、`af_c_id=0001`

任何内部 route 形状或目标不一致均 fail closed，不把 URL 交给前端。

## 错误响应

公开错误最小形状：

```json
{
  "found": false,
  "error": "invalid_request",
  "message": "Enter a four-character code or complete Content ID."
}
```

来自 sidecar 的稳定错误还会同时包含同值的 `code` 字段。

| HTTP | `error` / `code` | 条件 |
| ---: | --- | --- |
| 400 | `invalid_request` | 缺参、未知/重复参数、非法 query 或 source |
| 404 | `tt_code_not_found` | 四位 code 不存在 |
| 404 | `not_found` | route 对应剧或直接查询的剧不存在 |
| 429 | `rate_limited` | 既有 token bucket 拒绝 |
| 503 | `resolver_overloaded` | 既有 in-flight gate 已满 |
| 503 | `tt_post_service_unavailable` | sidecar 连接失败或 3 秒调用超时 |
| 503 | `resolver_unavailable` | DramaWave resolver 不可用 |
| 500 | `tt_post_internal_error` | sidecar 未预期的 SQLite/运行时错误，消息已清洗 |
| 500/502 | `tt_code_route_invalid` | sidecar 存储 route 或主 app 二次校验失败 |

Redis 失败本身不会产生 5xx；resolver 会回退 SQLite。错误响应不得泄露内部 bearer、Redis 地址/key、SQLite 路径、SQL 或堆栈。

## 私有 sidecar 接口

```http
GET http://127.0.0.1:18829/internal/tt-posts/code-resolve?query=<value>&source=Search|Featured
Authorization: Bearer <TT_POST_INTERNAL_TOKEN>
```

- 只允许 loopback，且 bearer 必须与主 app 配置匹配；未授权返回 403。
- 返回 route-only item：`content_id`、`target_url`、`query_type`、`route_mode`、可选 `code`、`source`、`af_channel`。
- 该接口不负责公共 DramaWave 元数据，也不应由 Nginx 暴露。
- 主 app 通过 `TT_POST_CODE_RESOLVER_TIMEOUT` 控制调用超时，示例和默认值为 3 秒。

## 发布侧合同

### `{code}`

正式自动/排期队列支持：

```text
{url}
{desc}
{code}
{{contect_id}}
{{content_id}}
```

所有新正式 queue 都先冻结 code/route，再一次渲染 caption；GPU 和 TikTok publish payload 只接收最终文本。预览显示 `A1B2`，不分配 code。直接测试使用 `{code}` 返回 `tt_post_code_macro_queue_only`。

历史无 code 队列和直接测试继续使用 `AIpost`；新正式队列明确使用 `TT`。

### queue 响应

queue 的安全 DTO 已加法包含 `code`。route 表的完整归因字段、Redis 信息与内部 token 不通过管理端或公共接口返回。

## Featured 数据

新页面仍用既有接口填充卡片：

```http
GET /api/public/tt-drama/featured
```

新页面只接受 schema 合法、未过期且恰好五条的数据；其他情况整体使用五条本地 fallback。卡片点击后只调用公共组合 resolver 一次。
