# API 文档

## 接口列表

### GPU 控制面

控制面继续只绑定 loopback，并通过既有 CPU↔GPU 隧道访问。路由不变：

| 方法 | 路径 | 说明 | 认证 |
| --- | --- | --- | --- |
| GET | `/health` | 无敏感信息的 worker、门禁和媒体后端状态 | loopback |
| POST | `/internal/tt-post/prepare` | 制作并固化成片，返回当前后端 URL | 内部 Bearer + 凭据合同 |
| POST | `/internal/tt-post/publish` | 门禁通过后以 `PULL_FROM_URL` 初始化发布 | 内部 Bearer + 短时 Token 信封 |
| POST | `/internal/tt-post/reconcile` | 按既有 `publish_id` 查询并固化远端状态 | 内部 Bearer + 短时 Token 信封 |

### GPU 媒体数据面

媒体服务使用独立 loopback 端口，由公网 HTTPS Nginx 只代理固定 prefix：

| 方法 | 路径 | 说明 | 认证 |
| --- | --- | --- | --- |
| GET | `/<prefix>/<job_id>/<sha256>/<signature>.mp4` | 完整或单段 Byte Range 读取 | HMAC URL 能力令牌 |
| HEAD | `/<prefix>/<job_id>/<sha256>/<signature>.mp4` | 返回与对应 GET 一致的状态和头，无 body | HMAC URL 能力令牌 |

首版不提供目录列表、manifest 查询、上传、删除、动态转码和多段 Range。
控制 API 不得通过公网媒体虚拟主机暴露。

## 请求/响应

### prepare

请求保持既有合同，关键字段示例：

```json
{
  "job_id": "tt-post:material:4665764:profile:...",
  "content_id": "Uj8akXa4XT",
  "source_url": "https://allowed-source.example/video.mp4",
  "expected_profile": "tt-post-hevc-720x1280-v2",
  "trim_tail_seconds": 4.333333
}
```

成功响应继续返回 CPU 已校验的不可变媒体字段，并可增加安全的后端标识：

```json
{
  "job_id": "tt-post:material:4665764:profile:...",
  "content_id": "Uj8akXa4XT",
  "prepared_media_url": "https://media.example.com/tt-post-media/v1/<job>/<sha>/<signature>.mp4",
  "prepared_media_sha256": "<64-lowercase-hex>",
  "prepared_media_size_bytes": 287559287,
  "prepared_media_duration_seconds": 2094.336,
  "prepared_media_mime": "video/mp4",
  "prepared_media_profile": "tt-post-hevc-720x1280-v2",
  "storage_backend": "local",
  "reused": false
}
```

`prepared_media_url` 必须与当前 manifest 中冻结 URL 完全一致；CPU 不自行
拼接或改写。响应不得包含本地文件路径、HMAC key、COS 密钥或 TikTok
Token。现有字段命名若仍使用 `url`/`sha256`/`size_bytes`，兼容层可以保留，
但同一个响应中不得出现相互矛盾的两套身份。

### 媒体 URL

规范路径：

```text
/<normalized-prefix>/<job_id>/<sha256>/<signature>.mp4
```

- `job_id`：服务端生成的安全、规范 segment，不接受路径分隔符或编码绕过。
- `sha256`：64 位小写十六进制，与文件和 manifest 完全一致。
- `signature`：对规范消息 `v1\n<job_id>\n<sha256>` 执行 HMAC-SHA256
  得到的 64 位小写十六进制；验证使用常量时间比较。prefix 由路由单独
  精确校验，不属于可变客户端输入。
- 签名不绑定短时过期，URL 生命周期由账本和文件删除控制，避免长排期
  提前失效。

正常完整 GET：

```http
HTTP/1.1 200 OK
Content-Type: video/mp4
Content-Length: 287559287
Accept-Ranges: bytes
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

单段 Range：

```http
Range: bytes=1048576-2097151
```

```http
HTTP/1.1 206 Partial Content
Content-Type: video/mp4
Content-Length: 1048576
Content-Range: bytes 1048576-2097151/287559287
Accept-Ranges: bytes
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

支持三种单段格式：

- `bytes=<start>-<end>`
- `bytes=<start>-`
- `bytes=-<suffix-length>`

`end` 超过文件结尾时截至 `size-1`；suffix 大于文件大小时返回全文件范围。
起始位置越界返回：

```http
HTTP/1.1 416 Range Not Satisfiable
Content-Range: bytes */287559287
Content-Length: 0
Accept-Ranges: bytes
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

非法语法、空范围、倒序或多段 Range 必须明确拒绝，不得忽略 Range 而返回
全量 `200`。`HEAD` 可以携带 Range；其状态和头与同请求的 GET 一致，但
body 始终为空。

### publish

local 与 COS 后端都继续使用相同 TikTok 请求语义：

```json
{
  "source_info": {
    "source": "PULL_FROM_URL",
    "video_url": "<exact prepared_media_url>"
  }
}
```

不得改成 `FILE_UPLOAD`。调用 init 前必须同时满足三项全局门禁、
`direct_post_eligible=true`，且 `TT_POST_URL_PROPERTY_VERIFIED_ORIGIN`
必须与当前后端规范化 origin 精确一致：local 使用
`TT_POST_GPU_LOCAL_MEDIA_ORIGIN`，COS 使用 `TT_POST_GPU_COS_DOMAIN`。
三个原门禁即使均为 1，origin 为空或错配时 `ready` 仍为 false。当前
品牌 profile 为 false，因此即使 URL 可读，也不能进入真实 init。
GPU publish 还会逐次解析 `prepared_media_url` 的实际 HTTPS origin；
只允许默认 443、无用户名/密码，且必须与 verified origin 精确一致。这样
即使后端切换或 manifest URL 被篡改，也会在 TikTok init 前拒绝。

### reconcile 与媒体生命周期

- 非终态/处理中：不修改媒体清理字段。
- init 或状态结果未知：写入 unknown/needs_review，禁止自动重试 init，
  禁止清理。
- 明确 `PUBLISH_COMPLETE` 或 `FAILED/PUBLISH_FAILED`：在
  `media_release` 中冻结 reason、
  `release_after_epoch = 当前时间 + grace` 和 `state=pending`。
- `init_outcome_unknown`/`init_rejected`/needs_review：首版不生成可自动
  清理状态；其中 5xx、408、409、425、429 必须归为 outcome unknown。
- 清理成功：幂等写入 `media_release.state=released` 与 `released_at`；
  保留 manifest/发布账本审计。
- 清理时身份不一致、文件类型异常或账本不完整：不删并进入人工核对。

## 错误码

以下为设计合同；实现可复用既有前缀，但语义必须稳定：

| HTTP | code | 场景 | 是否可自动重试 |
| --- | --- | --- | --- |
| 400 | `invalid_request` | 非法 job、SHA、Range 或请求字段 | 否 |
| 401/404 | `media_not_found` | 签名/路径/manifest/文件任一无效；公网响应避免区分细节 | 否 |
| 405 | `method_not_allowed` | 媒体端点收到 GET/HEAD 之外方法 | 否 |
| 409 | `prepare_idempotency_conflict` | 同 job 后端、URL、SHA、profile 或资产身份漂移 | 人工核对 |
| 409 | `prepared_artifact_not_found` | ready manifest 对应文件缺失 | 人工核对 |
| 416 | `range_not_satisfiable` | Range 起始越界 | 调整范围后可重试 |
| 500 | `prepared_media_invalid` | 文件 SHA/大小/probe/profile 与 manifest 不符 | 人工核对 |
| 423/409 | `publish_outcome_unknown` | init 结果未知或 needs_review | 只 reconcile，不重发 |
| 507 | `local_media_storage_full` | local 数据盘低于安全水位 | 扩容/清理明确终态后重试 |
| 503 | `blocked_compliance` | 任一发布门禁关闭 | 否，需审批 |
| 503 | `blocked_compliance` | verified origin 缺失或与当前后端 origin 不匹配 | 否，修正验证证据和 root-only 配置 |
| 403 | `tt_publish_url_property_mismatch` | 当前 prepared URL 的实际 origin 与 verified origin 不一致 | 否，人工核对 manifest/后端/Property |
| 403 | `tt_media_profile_not_direct_post_eligible` | 品牌媒体门禁关闭 | 否 |
| 500 | `invalid_configuration` | 当前后端配置缺失或不安全 | 修复配置并重启 |

公网媒体错误响应不得泄露 HMAC 校验结果、manifest、本地路径、完整 URL、
Token 或内部异常堆栈。

## 兼容性说明

- `TT_POST_GPU_STORAGE_BACKEND=cos` 时，现有 COS 上传、HEAD 校验和 URL
  语义保持不变。
- 配置校验按所选后端执行：COS 模式不要求 local key/origin；local 模式
  不要求 COS 凭据。
- ready manifest 冻结 `storage_backend`。worker 切换后端后，旧 manifest
  仍按原后端解释；不得把旧 COS URL 改写为 local，也不得反向改写。
- CPU 继续把 `prepared_media_url` 当作不透明 HTTPS URL，并复验
  job/content/profile/SHA/大小/时长。CPU 不需要知道媒体本地路径。
- 后端切换不会沿用旧后端的 URL Property 声明；
  `TT_POST_URL_PROPERTY_VERIFIED_ORIGIN` 必须随当前后端重新精确匹配，
  但未完成外部验证时不得仅修改环境值来伪造通过。
- 历史 queue、publish_id 和 unknown 账本不能因后端切换而重新 init。
- local 代码可以在三项门禁关闭时部署和做关闭态读取验收；只有 DNS、
  80/443、可信证书和 TikTok URL Property 全部有证据后，才可另行申请
  真实发布变更。
