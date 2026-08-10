# API 文档

## 接口列表

- `GET /2/users/me`：新增请求 `user.fields=subscription_type`（仍使用个人账号 OAuth token）。
- 现有 owner/admin X 账号列表与 verify 响应：增加安全会员字段。
- 现有排期账号选项：增加安全会员字段。
- `POST /internal/x-post-media-repair`：新增 `duration_policy`。

## 请求/响应

账号 DTO 新增：

```json
{
  "subscription_type": "premium_plus",
  "premium_subscriber": true,
  "long_video_eligible": true,
  "long_video_publish_eligible": true
}
```

`subscription_type` 仅为 `none|basic|premium|premium_plus|unknown`。最后一个字段还要求账号动态状态为 `active` 且 `publish_approved=true`。

GPU 修复请求新增：

```json
{
  "duration_policy": "premium"
}
```

只接受 `standard|premium`；固定 profile 为 `x-h264-nvenc-720-duration-policy-v3`，job key namespace 同步升为 v3，避免与旧 139 秒产物复用。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `x_long_video_requires_premium` | 原始时长超过 140 秒，但目标账号未明确具备会员权益 |
| `invalid_media_duration` | 时长低于 0.5 秒或超过当前账号/API 策略上限 |
| `profile_mismatch` | CPU/GPU 修复 profile 未同步部署 |

## 兼容性说明

- 字段与 SQLite 列均为加法；旧客户端可忽略。
- 历史账号迁移为 `unknown`，不会意外获得长视频权限。
- 历史队列时长为 `0`，发布时仍以真实下载文件探测结果和最新账号会员状态为准。
