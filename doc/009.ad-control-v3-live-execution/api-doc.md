# API 文档

## 接口列表

`POST /api/ad-control/v3/rule-groups/{group_id}/execute`

使用当前登录用户、规则组最新有效试算和服务端能力开关执行真实 Meta 动作。接口不接受 owner、账号、对象或动作覆盖。

## 请求/响应

请求：

```json
{"confirm":"EXECUTE_LIVE_RULE_GROUP","preview_id":"可选，若填写必须等于最新试算"}
```

成功响应包含 `execution_id/status/summary/targets`。`summary.meta_write_count` 是本次实际 Meta POST 数；每个 target 包含 `succeeded/skipped/failed`、稳定 reason，复制成功时含 `copy_intent_id` 和新对象映射。

`GET /api/ad-control/v3/meta` 新增：

```json
{
  "permissions": {
    "live_pause_enabled": true,
    "live_copy_enabled": true,
    "can_live_execute": true,
    "scheduler_enabled": true,
    "scheduler_live_enabled": true
  }
}
```

## 错误码

| code | 含义 |
| --- | --- |
| `live_mode_required` | 规则组不是正式执行模式 |
| `stale_preview` | 最新试算缺失、过期或行为 hash 不一致 |
| `live_execute_confirm_required` | 确认短语不匹配 |
| `live_pause_disabled` / `live_copy_disabled` | 对应总开关关闭 |
| `copy_schema_mismatch` | created_data 镜像结构漂移 |
| `missing_source_created_data` | 找不到可追溯来源行 |
| `meta_object_account_mismatch` / `meta_parent_mismatch` | Meta 归属回读不一致 |
| `carrier_budget_not_independent` | 承载结构无法独立设置预算 |
| `roas_bid_incompatible` | 来源不是兼容 MIN_ROAS 模式 |
| `duplicate_completed_intent` / `source_cooldown` | 幂等或冷却跳过 |
| `meta_transport_uncertain` | POST 响应不确定，intent 已隔离且不重试 |

## 兼容性说明

- 同源 JSON 与现有模块权限校验保持不变。
- 现有 preview 接口仍为零 Meta 写入。
- V2 路由和 cron 不变。
- TikTok 仍返回 channel not enabled。
