# 内部 API 合同

## 变更范围

无新增路由、无外部 AI 后台 API 字段变化。

## GPU prepare

`POST /internal/tt-post/prepare`

沿用现有请求字段。`source_direct` 时必须满足：

```json
{
  "expected_profile": "tt-post-source-direct-v1",
  "source_trim_tail_seconds": 0
}
```

成功响应沿用现有字段，并返回：

```json
{
  "direct_post_eligible": true,
  "media_mode": "source_direct",
  "profile": "tt-post-source-direct-v1",
  "transition": "none"
}
```

`output_sha256` 和 `output_size` 必须分别等于下载后原片的 SHA-256 和大小。`output_url` 指向已验证的拉取 origin，不是原始 COS URL。

## GPU health

`GET /health` 增加新的可观测取值：

- `media_mode=source_direct`
- `profile=tt-post-source-direct-v1`
- `transition=none`
- `direct_post_eligible=true`

## GPU publish

`POST /internal/tt-post/publish` 不变，仍使用 `PULL_FROM_URL`。发布前会重新验证 manifest v6、输出 SHA/大小、媒体合同、实际 URL origin 和三项生产门禁。
