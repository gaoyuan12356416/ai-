# API / 合约变更

## 文案宏

- 新增精确双花括号宏：`{{drama_name}}`。
- 值：素材/任务冻结时的真实 `drama_name`。
- 使用宏但值为空：`caption_drama_name_required`，HTTP 400/任务失败关闭。
- 未使用该宏：不要求剧名，兼容旧模板。

## GPU prepare

请求结构不增加客户端可控随机字段。`expected_profile` 在新模式必须为：

```text
tt-post-random-overlay-hevc-720x1280-v3
```

响应在新模式增加只读审计字段：

```json
{
  "recipe": {
    "asset_set_sha256": "<sha256>",
    "border": "border-1.png",
    "opacity_video": "opacity-video-1.webm",
    "corners": "corners-1.webm",
    "tint": "tint-white.png",
    "tint_opacity_bp": 750,
    "rotation_millidegrees": -750,
    "scale_bp": 10050
  }
}
```

客户端可忽略新增字段；GPU manifest 必须校验并复用。
