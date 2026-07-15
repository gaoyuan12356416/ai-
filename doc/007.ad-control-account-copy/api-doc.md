# API 文档

## 接口列表

| 方法 | 路径 | 用途 | 写外部系统 |
| --- | --- | --- | --- |
| GET | `/api/ad-control/accounts` | 跨产品聚合当前 FB created_data 中的账号 | 否 |
| GET | `/api/ad-control/rule-groups` | 当前登录用户的规则组列表 | 否 |
| POST | `/api/ad-control/rule-groups` | 新建或按 `group_id` 更新（upsert）本人规则组 | 仅 SQLite |
| DELETE | `/api/ad-control/rule-groups/{id}` | 软删除并禁用本人规则组 | 仅 SQLite |
| POST | `/api/ad-control/rule-groups/{id}/enabled` | 启停规则组 | 仅 SQLite |
| POST | `/api/ad-control/rule-groups/{id}/preview-live` | 一次性立即试算 | 仅 SQLite 审计 |
| POST | `/api/ad-control/rule-groups/{id}/execute-live` | 根据已确认预览执行；pause 可写 Meta，copy 本期固定失败关闭 | 视动作决定 |
| POST | `/api/ad-control/emergency-stop` | 全局或单组停止新动作 | 仅 SQLite |

## 请求/响应

保存规则组请求示例：

```json
{
  "group_id": "可选，更新时提供",
  "migrate_from_group_ids": [],
  "name": "爆款 Campaign 放量",
  "account_ids": ["1234567890"],
  "object_level": "campaign",
  "run_mode": "observe",
  "enabled": false,
  "schedule": {
    "type": "interval",
    "interval_minutes": 30,
    "allowed_start": "08:00",
    "allowed_end": "22:00",
    "timezone_source": "ad_account"
  },
  "limits": {
    "per_rule_daily": 1,
    "per_user_daily": 10,
    "source_cooldown_days": 1
  },
  "candidate_selection": {
    "mode": "top_n_per_account",
    "top_n": 1,
    "order_by": ["roas_desc", "spend_desc", "object_id_asc"]
  },
  "rules": [
    {
      "rule_id": "best-roas-copy",
      "name": "最佳 ROAS",
      "enabled": true,
      "action": "copy",
      "conditions": [{"field": "roas", "operator": ">=", "value": 1.5}],
      "drama_scope": {"type": "recent_days", "days": 7},
      "copy": {
        "budget": {"type": "actual_cpi_multiplier", "multiplier": 20},
        "roas_bid": {"direction": "decrease", "percent": 10},
        "final_status": "ACTIVE"
      }
    }
  ]
}
```

安全规则：

- `owner_user_id` 不属于公开请求契约；若传入且与当前用户不一致则拒绝。
- 新建时后端强制 `enabled=false`、默认 `run_mode=observe`。
- 更新仍使用集合路径 `POST /api/ad-control/rule-groups`，以 body 中的 `group_id` 定位；不存在 `POST /api/ad-control/rule-groups/{id}` 更新路由。
- `migrate_from_group_ids` 仅用于把旧前端聚合组收敛为新账号级组，只提交旧底层组 ID，不包含新的 `group_id`。后端校验这些旧组均属于当前用户和同一 `frontend_rule_group_id`，随后在同一 SQLite 事务中保存新组并禁用、软删除旧组。
- 将已有规则组切换到 `run_mode=live` 时必须额外传 `live_mode_confirm=ENABLE_LIVE_MODE`。
- 本期 `object_level=ad` 只允许保存配置；规则组不能启用，立即试算、runner 和正式执行均返回 `phase_not_enabled`，不会复用 Campaign 候选。
- `preview-live` 永远不调用 Meta 写接口或复制结果写入；它会写 SQLite preview/审计元数据，并返回 Campaign 对象的决策、命中规则、shadowed 规则和 skip reason。Ad 不返回候选。
- 本期正式 `copy` 固定在任何 Meta POST 前返回 `copy_persistence_not_configured`；即使复制总熔断被误开也不得越过此前置条件。
- 复制总熔断与 pause 独立；既有正式 pause 继续按原路径执行。

执行响应的关键字段：

```json
{
  "action_id": "...",
  "preview_id": "...",
  "run_mode": "observe",
  "requested_count": 1,
  "success_count": 0,
  "skipped_count": 1,
  "error_count": 0,
  "results": [
    {
      "object_key": "fb:campaign:123",
      "status": "observed",
      "reason": "would_copy"
    }
  ]
}
```

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `owner_forbidden` | 请求尝试读写其他用户的规则组 |
| `invalid_object_level` | 调控对象不是 campaign/ad |
| `invalid_rule_action` | 规则动作不是 pause/copy |
| `live_mode_confirm_required` | 切正式模式缺少二次确认 |
| `preview_required` / `preview_hash_mismatch` | 正式执行前未完成一致的试算确认 |
| `copy_persistence_not_configured` | 本期未接入复制结果持久化，正式复制在 Meta 写入前失败关闭 |
| `phase_not_enabled` | Ad 候选、启用、试算、runner 和正式执行均未开放；本期仅可保存配置 |
| `quota_exceeded` | 规则、用户或部署日额度已满 |
| `source_cooldown` | 同一来源仍处于冷却期 |
| `unknown_account_timezone` | 无法确定账号时区 |
| `unsupported_roas_bid_strategy` | 来源竞价模式不允许调整 ROAS |
| `copy_mapping_mismatch` | 隔离编排校验中来源与复制对象无法一一映射；本期生产路径不会到达此阶段 |

## 兼容性说明

- 旧规则组在 SQLite 迁移后仍保留 product 作为内部历史元数据，但新 UI/API 不再提供产品维度。
- 旧 `pause` Campaign 规则继续执行；迁移不会自动打开任何复制规则。
- 前端旧聚合组列表根据底层组真实 enabled 状态计算并保留 `partial_enabled`；编辑时由 UI 提交 `migrate_from_group_ids`，成功后旧底层组才会在同一事务中禁用和软删除。
- 旧 `action=observe` 编辑时显式迁移为 `run_mode=observe` + `action=pause`；其他未知 action 返回 `invalid_rule_action`，不得静默转成 pause。
- Token 和账号池管理页仍可保留产品维度作为凭据/元数据管理，不再决定规则组筛选范围。
- 本期候选只读取现有 FB 原始发布数据；复制数据回流和再次扫描随用户后续指定的复制结果 ads_ai 写入方案实现。
- 本期不新增、不迁移、不写 copied created_data/lineage/intent 表；既有 action log 审计不变。
