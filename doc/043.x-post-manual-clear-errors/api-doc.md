# API 文档

## 接口列表

- `GET /api/admin/x-posts/material-pool/manual-runs/{id}`：结构不变。
- GPU `/internal/x-post-media-repair`：结构和 HTTP 合同不变。

## 请求/响应

手动 run 仍返回：

```json
{
  "status": "failed_preflight",
  "error_code": "repaired_media_too_large",
  "error_message": "素材 6179846：修复后视频超过512MB上限"
}
```

页面不直接展示 `error_code`，只展示中文“拦截原因”。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `material_not_found` | 素材不存在 |
| `material_not_video` | 素材不是视频 |
| `material_inactive` | 素材已删除或不可用 |
| `material_duration_invalid` | 时长字段无效 |
| `material_duration_missing` | 时长缺失或为0秒 |
| `material_duration_exceeds_limit` | 源时长超过入口上限 |
| `repaired_media_empty` | manual runner 确认修复产物为空 |
| `repaired_media_too_large` | manual runner 确认修复产物超过512MB |
| `repaired_media_missing` | manual runner 确认修复产物不存在 |
| `repaired_media_duration_invalid` | 修复产物时长不合规 |
| `repaired_media_duration_mismatch` | 修复产物时长与预期不一致 |

## 兼容性说明

- 无 schema、请求字段或响应字段变更。
- GPU worker 继续使用原有 `repaired_media_invalid` 协议码，具体中文消息由 manual runner 归一为上表细码，避免影响自动/短剧池既有 allowlist。
- 页面兼容历史 `material_not_found_or_ineligible` 和 `repaired_media_invalid`。
