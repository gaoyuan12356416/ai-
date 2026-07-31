# API 文档

## 接口列表

### 管理页读取

`GET /api/admin/tt-posts/schedule?source_account_id={id}`

现有接口新增：

```json
{
  "item": {
    "can_publish_now": true,
    "manual_canary_ready": true,
    "manual_canary": {
      "enabled": true,
      "ready": true,
      "privacy_level": "SELF_ONLY",
      "test_bypass": true
    }
  },
  "gates": {
    "live_enabled": false,
    "direct_audit_approved": false,
    "url_property_verified": false
  }
}
```

目标账号/素材不匹配、素材已领取或许可过期时，`manual_canary_ready=false`。

### 人工触发

`POST /api/admin/tt-posts/run-now`

请求结构不变：

```json
{
  "source_account_id": "640",
  "idempotency_key": "浏览器生成并持久化的请求键"
}
```

浏览器不能提交 Job、媒体 URL、隐私级别、互动开关或 access token。服务端从已冻结的目标与许可配置派生。

### CPU 到 GPU

普通路径保持：

`POST /internal/tt-post/publish`

一次性测试使用独立路径：

`POST /internal/tt-post/canary-publish`

该接口只监听 GPU loopback，通过专用 bearer；credential envelope 的 operation 固定为 `canary_publish`，不能在普通 publish 路径重放。

## 错误码

| 错误码 | 含义 | 是否调用 TikTok |
| --- | --- | --- |
| `tt_post_live_gates_closed` | 普通发布门禁关闭 | 否 |
| `tt_post_manual_canary_config_invalid` | CPU 白名单配置无效 | 否 |
| `tt_post_manual_canary_target_mismatch` | CPU 下一条素材不匹配 | 否 |
| `tt_post_manual_canary_schedule_locked` | 测试期间尝试启用每日排期 | 否 |
| `tt_post_manual_canary_identity_mismatch` | 冻结队列身份不匹配 | 否 |
| `tt_manual_canary_closed` | GPU 许可关闭或过期 | 否 |
| `tt_manual_canary_target_mismatch` | GPU 请求目标/策略不匹配 | 否 |
| `tt_manual_canary_artifact_mismatch` | GPU manifest、SHA、大小、profile、origin/path 不匹配 | 否 |
| `tt_upstream_rejected` | TikTok 明确拒绝 | 是，且不重试 |
| `tt_upstream_unavailable` | TikTok 结果未知 | 是，进入 unknown，不重试 |
| `tt_publish_reconcile_required` | 已取得 publish ID | 否，只允许 reconcile |
| `tt_publish_retry_blocked` | 已存在无安全重试路径的 init 账本 | 否 |

## 上游错误保真

GPU 只保留经过凭据清洗的字段：

- HTTP status
- TikTok `error.code`
- TikTok `error.message`
- TikTok `error.log_id`
- 接收时间
- 是否发生脱敏

不保存完整响应、响应头、token、credential envelope 或签名 URL。

## 兼容性说明

- 正式三重门禁的请求/响应与语义不变。
- 普通发布继续走原 GPU 路由和 `publish` credential operation。
- 数据库无 schema 变更。
- 白名单未启用或已过期时，行为与变更前一致。
