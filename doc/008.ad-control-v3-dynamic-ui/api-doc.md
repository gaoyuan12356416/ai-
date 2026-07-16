# 自动调控 V3 API 文档

## 1. 通用契约

- 根前缀：`/api/ad-control/v3`。
- V3 复用已有 Nginx `/api/ad-control/` 反向代理，不新增 location。
- UI/asset 仅接受 cookie 登录；所有端点要求 `ad_control_center` 模块权限。
- `POST/PUT/DELETE` 要求同源、`Content-Type: application/json`，请求体上限 2 MiB。
- JSON 使用 UTF-8、`Cache-Control: no-store`。动态 HTML 额外包含 CSP、`Referrer-Policy: same-origin` 和 `nosniff`。
- 成功响应直接返回业务对象；创建返回 201，其余 200。
- 未知异常只返回 `500/internal_error`，不泄露 SQL、文件绝对路径、Cookie、Token 或堆栈。

V3 payload 禁止以下任意嵌套范围字段：`account_id(s)`、`accounts`、`ad_account_id(s)`、`account_group_id(s)`、`account_pool_id(s)`。命中返回 `account_scope_forbidden`。

错误结构：

```json
{
  "ok": false,
  "error": "validation_error",
  "message": "request is invalid",
  "details": {"field": "products"}
}
```

## 2. 动态页面与资产

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/ui/rule-groups` | 动态规则组管理页 |
| GET | `/ui/execution-logs` | 动态执行日志页 |
| GET | `/assets/app.css` | allowlist CSS |
| GET | `/assets/app.js` | allowlist JavaScript |

以上路径均拼接根前缀。页面 bootstrap 只包含 `apiBase` 和页面名；任意未知模板、asset 或路径穿越返回 V3 JSON 404。本期不发布 V3 静态 HTML。

## 3. 元数据

### `GET /api/ad-control/v3/meta`

返回结构与冻结实现一致：

```json
{
  "actor": {
    "user_id": "892fd2e8",
    "name": "示例用户",
    "email": "user@example.com",
    "role": "admin",
    "is_admin": true,
    "optimizer_id": null
  },
  "channels": [
    {
      "channel": "facebook",
      "enabled": true,
      "object_levels": ["campaign", "adset", "ad"],
      "observe": true,
      "live_pause": false,
      "live_copy": false
    },
    {
      "channel": "tiktok",
      "enabled": false,
      "reason": "channel_not_enabled"
    }
  ],
  "object_levels": [
    {"value": "campaign", "label": "Campaign"},
    {"value": "adset", "label": "Ad Set"},
    {"value": "ad", "label": "Ad"}
  ],
  "run_modes": [
    {"value": "observe", "label": "只观察", "enabled": true},
    {"value": "live", "label": "正式执行", "enabled": false, "reason": "live_pause_disabled"}
  ],
  "actions": [
    {"value": "pause", "observe_ready": true, "live_ready": false},
    {"value": "copy", "observe_ready": true, "live_ready": false}
  ],
  "field_catalog": {
    "campaign": [],
    "adset": [],
    "ad": []
  },
  "products": [
    {"channel": "facebook", "product_value": "Dramawave", "enabled": 1}
  ],
  "optimizers": [],
  "account_timezones": [],
  "permissions": {
    "can_select_optimizer": true,
    "can_enable": false,
    "scheduler_enabled": false,
    "live_pause_enabled": false,
    "live_copy_enabled": false,
    "tiktok_enabled": false
  },
  "defaults": {"enabled": false, "run_mode": "observe"}
}
```

普通用户的 `actor.optimizer_id` 为服务端唯一映射结果，`optimizers` 只返回本人并标记 locked。字段项包含 `key/label/value_type/levels/source/operators/filterable/previewable/live_ready/options`。UI 只能选择 `filterable=true && previewable=true` 的字段。

## 4. 规则组 CRUD

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/rule-groups` | 服务端分页列表 |
| POST | `/rule-groups` | 创建 disabled/observe 草稿 |
| GET | `/rule-groups/{group_id}` | 详情 |
| PUT | `/rule-groups/{group_id}` | 乐观锁更新 |
| DELETE | `/rule-groups/{group_id}` | 软删除，body 为 `{}` |
| POST | `/rule-groups/{group_id}/duplicate` | 复制为新 disabled/observe 草稿 |

列表参数：`page`、`page_size`（1～100）、`keyword|query`、`optimizer_id`、重复 `product`/`products`、`channel`、`object_level`、`run_mode`、`enabled`。普通用户跨 optimizer 查询返回 `optimizer_forbidden`。

### 4.1 创建/更新 payload

下面是合法结构示例，示例值不是默认值：

```json
{
  "name": "Dramawave 高 ROAS 观察",
  "description": "",
  "channel": "facebook",
  "optimizer_id": 248,
  "products": ["Dramawave", "[w2a]FreeReels-double"],
  "account_timezones": [],
  "object_level": "campaign",
  "run_mode": "observe",
  "rules": [
    {
      "rule_id": "high-roas",
      "name": "高 ROAS",
      "priority": 10,
      "logic": "and",
      "action": "copy",
      "conditions": [
        {"field": "spend", "operator": "gte", "value": 100},
        {"field": "roas", "operator": "gte", "value": 1.2},
        {"field": "series_code", "operator": "in", "value": ["SERIES-001"]}
      ],
      "copy_parameters": {
        "carrier_strategy": "deep_copy_campaign",
        "budget_mode": "actual_cpi_multiplier",
        "budget_multiplier": 10,
        "roas_adjustment_direction": "decrease",
        "roas_adjustment_percent": 10,
        "cooldown_days": 1,
        "daily_copy_limit": 1
      }
    }
  ],
  "schedule": {
    "type": "interval",
    "interval_minutes": 30,
    "allowed_start_time": "08:00",
    "allowed_end_time": "22:00"
  },
  "quotas": {
    "group_daily_limit": 1,
    "user_daily_limit": 10,
    "object_cooldown_days": 1
  },
  "selection": {
    "mode": "account_top_n",
    "metric_window_days": 1,
    "top_n": 1,
    "sort_field": "roas",
    "sort_direction": "desc"
  }
}
```

约束：

- 创建时服务端忽略/拒绝 server-managed 字段并强制 `enabled=false, run_mode=observe`。
- 产品 1～20 个精确枚举；源查询同时使用可索引等值和 BINARY exact，V3 product 列为 binary collation；普通用户 optimizer 必须为本人，admin 目标必须为 active optimizer。
- `selection.metric_window_days` 必填，1～31。
- `selection.mode`：`all | account_top_n | product_top_n | global_top_n`；Top N 模式须提供 `top_n/sort_field/sort_direction`。
- Copy carrier：Campaign=`deep_copy_campaign`；Ad Set=`same_campaign|new_campaign`；Ad=`same_adset|isolated_adset|isolated_campaign`。
- Copy budget mode：`actual_cpi_multiplier | fixed_target_cpi_multiplier | source_budget_ratio`。
- 更新必须提供 `If-Match: "{config_version}"` 或 body 正整数 `version`；冲突返回 `version_conflict`。`version` 不进入持久化配置。
- 更新后规则总是 disabled，并清空旧 Preview pointer。

## 5. 范围估算与手动试算

### 5.1 `POST /scope-estimate`

```json
{
  "channel": "facebook",
  "optimizer_id": 248,
  "products": ["Dramawave"],
  "account_timezones": [],
  "object_level": "adset",
  "metric_window_days": 2
}
```

也可使用成对 `date_from/date_to`（`YYYY-MM-DD`）替代 `metric_window_days`；两套窗口不能缺半边。返回 `scope/account_count/object_count/eligible_object_count/blocked_count/blocked_reasons/live_ready=false`。结果仅用于估算，不形成用户可选择的账号列表。

### 5.2 `POST /rule-groups/{group_id}/preview`

body 可为：

```json
{"metric_window_days": 2, "trigger_source": "manual_preview"}
```

或显式 `date_from/date_to`。若省略窗口，使用规则组 `selection.metric_window_days`。

成功响应：

```json
{
  "preview_id": "...",
  "execution_id": "...",
  "status": "ready",
  "expires_at": "2026-07-16T03:00:00Z",
  "summary": {"meta_write_count": 0},
  "targets": [],
  "target_count": 0,
  "truncated": false,
  "live_ready": false
}
```

Preview 会写数据盘快照及 `ads_ai` Preview/Execution/Target 行；不会 lookup Token 或调用 Graph。

## 6. 状态操作

| 方法 | 路径 | 本期行为 |
| --- | --- | --- |
| POST | `/rule-groups/{id}/enabled` | `enabled=false` 可停用；`enabled=true` 固定失败关闭 |
| POST | `/rule-groups/{id}/emergency-stop` | 停用该组、设置 emergency 并清空 Preview pointer |

启用请求必须使用 JSON boolean：

```json
{"enabled": true, "confirm": ""}
```

本期没有 live 二次确认成功路径：

- observe enable：`runner_scheduler_not_configured`；
- live pause：`live_pause_disabled`；
- live copy：`copy_persistence_not_configured`；
- TT：`channel_not_enabled`。

这些门禁发生在 Token/Graph 前。`confirm` 为后续 live 版本预留，本期不能绕开门禁。

## 7. 执行日志

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/executions` | 服务端分页查询 V3 手动 observe 事件 |
| GET | `/executions/{execution_id}` | 摘要、目标和已校验快照 header |

列表筛选：`page/page_size`、`date_from/date_to`、`optimizer_id`、重复 `product/products`、`rule_group_id`、`channel`、`object_level`、`action`、`run_mode`、`status`、`trigger`、`object_id`、`keyword/query`。

普通用户始终限本人 optimizer。详情按 execution ID 查询，跨 optimizer 返回 404。快照只接受数据库保存的相对路径和 SHA-256，客户端不能提交路径。

## 8. 主要错误码

| HTTP | error | 含义 |
| --- | --- | --- |
| 400 | `validation_error` / `invalid_json` / `invalid_request` | 请求契约错误 |
| 400 | `account_scope_forbidden` | 出现账户/账户池范围字段 |
| 400 | `field_not_supported` / `operator_not_supported` | 字段或操作符不可试算 |
| 403 | `permission_denied` / `cookie_auth_required` | 认证或模块权限不足 |
| 403 | `optimizer_forbidden` / `optimizer_identity_unresolved` | optimizer 越权或无法唯一解析 |
| 404 | `not_found` / `rule_group_not_found` / `execution_not_found` | 不存在或不可见 |
| 405 | `method_not_allowed` | 方法不允许，带 `Allow` |
| 409 | `version_conflict` / `stale_preview` | 版本或 Preview 冲突 |
| 409 | `channel_not_enabled` | TT/未知渠道未发布 |
| 409 | `runner_scheduler_not_configured` | scheduler 未发布 |
| 409 | `live_pause_disabled` | live pause 未发布 |
| 409 | `copy_persistence_not_configured` | live copy/落表合同未发布 |
| 409 | `snapshot_missing/hash_mismatch/invalid` | 快照验证失败 |
| 413 | `snapshot_too_large` | 快照超过限制 |
| 500 | `internal_error` | 未知内部错误 |
| 503 | `service_not_configured` / `unsafe_mysql_endpoint` / `unsafe_data_root` | 生产依赖未安全配置 |
| 507 | `data_disk_low_space` | 数据盘余量不足 |

## 9. 兼容性

- `app.py` 仅在 path 等于 `/api/ad-control/v3` 或以该前缀开头时懒加载 V3。
- `PUT` 沿用 monolith 的 `do_PUT -> do_POST`，V3 route 读取原始 method。
- 未知 V3 路径返回 V3 JSON 404，不落入 V2。
- 本 API 不读写 V2 SQLite，也不创建 copied `created_data/lineage/intent`。
