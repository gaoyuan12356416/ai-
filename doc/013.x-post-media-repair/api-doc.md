# 013.x-post-media-repair API 文档

## GPU 修复接口

`POST /internal/x-post-media-repair`

- 仅监听 `127.0.0.1:8820`。
- CPU 通过 `127.0.0.1:18820` 反向隧道访问。
- `Authorization: Bearer <独立修复 Token>`。
- 仅接受 `invalid_media_codec`、`invalid_media_dimensions`。

请求：

```json
{
  "job_key": "<64位小写sha256>",
  "material_id": "5779172",
  "pool_item_id": "8",
  "source_url": "https://allowed-cos-host/source.mp4",
  "source_sha256": "<64位小写sha256>",
  "source_size": 17879161,
  "trigger_code": "invalid_media_codec",
  "profile": "x-h264-nvenc-720-v1"
}
```

`material_id` 仅允许两种互不重叠的源身份：原素材池的正十进制 ID，或
`ads_drama_resource.id` 的 32 位小写十六进制 ID。后者只用于 GPU 修复源身份
和 COS 路径，不替代短剧发布的 `episode_key` 排重；`pool_item_id` 始终保持正
十进制短剧池/素材池记录 ID。大写十六进制、路径字符和其他自由文本均拒绝。

成功响应：

```json
{
  "status": "ready",
  "reused": false,
  "job_key": "<与请求一致>",
  "profile": "x-h264-nvenc-720-v1",
  "output_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/x-post-media-repair/...",
  "output_sha256": "<64位小写sha256>",
  "output_size": 12345678,
  "probe": {
    "codec": "h264",
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "width": 720,
    "height": 1280,
    "frame_rate": 30.0,
    "duration": 119.234,
    "size": 12345678
  }
}
```

健康检查：`GET /health`，仅回传状态与 profile，不回传配置或凭证。

## Daily 冻结计划查询

`POST /internal/posts/daily-plan/query`

- 仅 loopback internal bearer；daily bearer 只能访问该精确路由。
- 请求仅允许 `{"run_date":"YYYY-MM-DD"}`。
- 返回 run 和 queue 身份字段，不包含文案、素材 URL、短链或 Token。
- 用于同日进程重启/响应丢失后的幂等读回；不创建或修改计划。

## 错误原则

- 非修复范围、源指纹变化、COS 校验失败、输出不合规或回执不一致均 fail closed。
- worker 不递归修复；daily 对单素材最多调用一次。
- Create Post 未知结果继续沿用原有禁止自动重发规则。
