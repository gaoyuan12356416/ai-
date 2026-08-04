# API 文档

## 状态

合同已冻结，代码与实测待完成。示例 ID、名称、时间和 URL 均为测试占位，不是生产凭据。

## 新公开接口

```http
GET /api/public/tt-code/resolve?query=<code-or-content_id>&source=Search|Featured
```

### 访问与缓存

- 无登录公开 GET，沿用 TT public resolver 的限流、并发上限和超时。
- 只接受精确两个 query 参数 `query`、`source`，各出现一次；未知/重复参数拒绝。
- 响应包含 `Cache-Control: no-store`。
- 可返回 `X-TT-Code-Cache: HIT|MISS|BYPASS`；该头只描述 code-route 读缓存，不得泄露 Redis 配置。
- 原 `GET /api/public/tt-drama/resolve` 和 `GET /api/public/tt-drama/featured` 保持原合同。

## 请求参数

| 参数 | 必填 | 规则 |
| --- | --- | --- |
| `query` | 是 | trim 后若为四位 ASCII 字母数字则作为 code 并转大写；否则必须满足现有完整 `content_id` 规则且保持大小写 |
| `source` | 是 | 精确枚举 `Search` 或 `Featured`，区分大小写 |

页面搜索框始终发送 `source=Search`；Featured 卡片始终发送 `source=Featured`。code exact 的冻结 `af_channel=TT` 不受 source 覆盖。

## 成功响应

顶层与 item 形状：

```json
{
  "found": true,
  "item": {
    "content_id": "LZ4b4w5k3h",
    "title": "Example Drama",
    "description": "Example description",
    "language": "en",
    "episode_count": 60,
    "cover_url": "https://cdn.example/cover.jpg",
    "target_url": "https://www.dramawavew2a.com/ads/101/2250/view?...",
    "query_type": "code",
    "route_mode": "code_exact",
    "code": "AB12"
  }
}
```

### 枚举

| 字段 | 枚举 | 含义 |
| --- | --- | --- |
| `query_type` | `code` | 四位 code 查询，输入统一大写 |
| `query_type` | `content_id` | 完整剧 ID 查询 |
| `route_mode` | `code_exact` | 按主键命中该 code 的冻结 URL，不受 queue 当前状态限制 |
| `route_mode` | `published_clone` | 命中同剧最新 published 路由并只替换 channel |
| `route_mode` | `generic_fallback` | 同剧无 published 路由，使用旧 generic 参数加入口 channel |

`code` 只在 `query_type=code` 时返回；content ID 查询应省略或返回空值，前后端实现需固定一种并测试。

## 路由规则

### 1. code exact

请求：

```http
GET /api/public/tt-code/resolve?query=ab12&source=Search
```

处理：

1. `ab12 -> AB12`。
2. 只按主键查 `tt_post_code_route.code='AB12'`，不以 `state` 过滤。
3. 用现有剧 resolver 确认 `content_id` 并补齐公开剧元数据。
4. 返回数据库冻结 `target_url`；`af_channel` 保持 `TT`，不改为 Search。

响应关键字段：

```json
{
  "found": true,
  "item": {
    "content_id": "LZ4b4w5k3h",
    "target_url": "https://www.dramawavew2a.com/ads/101/2250/view?af_dp=LZ4b4w5k3h&c=yingliang_post_CLV_VL_creator%2A...%2A101&af_adset=Page&af_adset_id=640&af_ad=Material_contentid%5BLZ4b4w5k3h%5D&af_ad_id=5801636&af_channel=TT&af_c_id=101",
    "query_type": "code",
    "route_mode": "code_exact",
    "code": "AB12"
  }
}
```

具体 URL encoder 可保留业务分隔星号，但解码后的 `c` 必须严格符合需求。

### 2. 直接 content ID：published clone

请求：

```http
GET /api/public/tt-code/resolve?query=LZ4b4w5k3h&source=Search
```

处理：

1. 先确认剧存在。
2. 在同剧 published route 中按 `published_at DESC, queue_id DESC` 取一条。
3. 克隆该行所有归因字段，唯一变化是 `af_channel=Search`。
4. 不新增或修改 `tt_post_code_route`。

响应：`query_type=content_id`、`route_mode=published_clone`。

### 3. Featured：published clone

```http
GET /api/public/tt-code/resolve?query=LZ4b4w5k3h&source=Featured
```

选择规则同上，唯一变化是 `af_channel=Featured`。响应为 `query_type=content_id`、`route_mode=published_clone`。

### 4. generic fallback

当 content ID 有效且剧存在，但没有任何 published route：

```text
https://www.dramawavew2a.com/ads/101/2250/view
  ?af_dp=<content_id>
  &c=TTpost
  &af_c_id=0001
  &af_channel=Search|Featured
```

响应为 `query_type=content_id`、`route_mode=generic_fallback`。fallback 不伪造 `af_adset`、素材或 queue 信息。

## 目标 URL 校验

返回前必须逐项验证：

- scheme `https`
- hostname 精确 `www.dramawavew2a.com`
- 无 username/password、自定义端口和 fragment
- path 精确 `/ads/101/2250/view`
- query 参数无关键字段重复
- `af_dp` 精确等于解析出的 `content_id`
- code exact 的 channel 为 `TT`
- published clone 的 channel 精确等于请求 source
- generic fallback 的 `c=TTpost`、`af_c_id=0001` 和 source 正确

校验失败必须 500/503 fail closed，不得把不可信 URL 交给前端。

## 未找到响应

未知 code 或不存在的剧：

```json
{
  "found": false,
  "error": "not_found",
  "message": "Story not found"
}
```

HTTP 404。命中 code 时不得返回或暗示 queue 内部状态；`unknown` 等已冻结 route 仍可搜索，以保护可能已经存在的帖子。

## 错误响应

统一形状：

```json
{
  "found": false,
  "error": "invalid_query",
  "message": "Invalid search value"
}
```

| HTTP | `error` | 条件 |
| ---: | --- | --- |
| 400 | `invalid_query` | query 既不是四位 code，也不是合法完整 content ID |
| 400 | `invalid_source` | source 不是精确 `Search|Featured` |
| 400 | `invalid_request` | 缺参、重复关键参数或未知参数 |
| 404 | `not_found` | code 不存在，或剧不存在 |
| 429 | `rate_limited` | 超过 token bucket |
| 503 | `resolver_busy` | 并发上限已满 |
| 503 | `resolver_unavailable` | SQLite 或剧 resolver 故障/超时 |
| 500/503 | `target_invalid` | 冻结/克隆目标未通过安全校验 |

Redis 故障本身不得产生 5xx；只有 SQLite 事实源或剧 resolver 也失败时才返回 503。

## 发布侧加法合同

### `{code}`

允许的精确 single-brace 宏增加：

```text
{url}
{desc}
{code}
```

Drama ID 双花括号别名保持：

```text
{{contect_id}}
{{content_id}}
```

queue 最终渲染前 code 必须已冻结。GPU 和 TikTok publish payload 只接收完全渲染 caption，不解释宏。

### queue/list 响应

建议加法公开安全字段：

```json
{
  "id": 101,
  "content_id": "LZ4b4w5k3h",
  "code": "AB12",
  "code_route_status": "published"
}
```

不得返回 Redis key、缓存原文、内部 token、SQLite 路径或未清洗异常。

## Featured 数据接口

新页面继续读取既有：

```http
GET /api/public/tt-drama/featured
```

新页面只接受 schema 合法、未过期且 `items.length === 5` 的结果；任何其他数量整体降级为恰好五条本地 fallback，不拼接成 4/6 条。
