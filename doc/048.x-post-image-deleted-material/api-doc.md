# API 文档

现有管理 API 请求/响应结构不变。

- `POST /api/admin/x-posts/material-pool`：活动图片及软删除视频可返回 `available_count`。
- `GET /api/admin/x-posts/material-pool`：历史错误在重检前保留，成功后变为 `available`。
- 内部 X 媒体上传：图片使用 `tweet_image`，GIF 使用 `tweet_gif`；视频继续使用 `tweet_video|amplify_video`。

管理路由继续要求管理员 Cookie；内部上传路由仍只允许 loopback bearer。

## 接口列表

## 请求/响应

## 错误码

## 兼容性说明
