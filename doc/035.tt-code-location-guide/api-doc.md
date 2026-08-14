# API 文档

## 接口列表

本需求不新增或修改 API。

- `GET /api/public/tt-code/resolve`：保持现有 code/Content ID 解析合同。
- `GET /api/public/tt-drama/featured-by-language/{lang}.json`：保持现有 schema v3 合同。
- `GET /tt-drama-code-assets/tt-code-location-guide.0b42fbc64ab4.webp`：新增同源静态图片资源。

## 请求/响应

WebP 静态资源成功响应：

- `200 OK`
- `Content-Type: image/webp`
- `Cache-Control: public, max-age=31536000, immutable`

## 错误码

- 图片路径不匹配或文件缺失：404。
- 图片 404 不改变 Resolver 或 Featured 的响应。

## 兼容性说明

- `/tt`、`/tt-code` 和 `/tt/` 路径行为不变。
- CSP 已允许同源 `img-src 'self'`，无需放宽安全策略。
- Resolver、W2A target 和八字段归因未改动。
