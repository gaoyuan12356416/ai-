# API 文档

## 接口列表

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/ad-control/actions` | 轻量日志列表，不返回原始 results |
| GET | `/api/ad-control/actions/{action_id}/targets` | 单条日志目标明细与原始结果 |
| POST | `/api/ad-control/rule-groups/{id}/preview-live` | Fresh live preview 与公平批次计划 |
| POST | `/api/ad-control/rule-groups/{id}/execute-live` | 执行已确认 preview |

均要求后台登录并拥有 `ad_control_center` 权限。

## 请求/响应

### GET actions

查询参数：`product`、`binding_id`、`action`、`date_from`、`date_to`、`limit(1..200)`、`view(daily|raw)`、`include_targets`。前端默认 `view=daily` 且固定传 `include_targets=false`；省略 `view` 时后端保持 `raw` 兼容行为。

新增/强调响应字段：

`view=daily` 响应示例：

```json
{
  "storage": "ads_ai",
  "storage_error": "",
  "view": "daily",
  "truncated": false,
  "raw_action_count": 2,
  "group_count": 1,
  "items": [{
    "group_id": "daily:2026-07-15:dramawave:rule-group:pause:campaign:real",
    "business_date": "2026-07-15",
    "is_daily_group": true,
    "batch_count": 2,
    "execution_batch_count": 2,
    "verification_batch_count": 0,
    "attempt_count": 386,
    "scanned_count": 1940,
    "matched_count": 386,
    "success_count": 355,
    "skipped_count": 31,
    "error_count": 0,
    "remaining_count": 0,
    "display_status": {"key": "success", "label": "当日执行完成", "class": "ok"},
    "log_store": "ads_ai",
    "results": [],
    "batches": [{
      "action_id": "...",
      "preview_id": "...",
      "requested_count": 200,
      "success_count": 169,
      "skipped_count": 31,
      "error_count": 0,
      "remaining_count": 195,
      "status": {"key": "partial", "label": "处理中，待续跑 195", "class": "warn"}
    }]
  }]
}
```

日卡累计成功/跳过/失败均为“执行尝试（含重试）”，不是唯一目标数。`scanned/candidate/matched/remaining` 使用首轮或最终快照，不跨批次求和。

`view=raw` 返回 action 级记录，最大 200 条，保留原字段兼容性。两种视图的列表都不读取 `results_json`。

日期按 Asia/Shanghai 业务日解释。daily 模式会前后扩展一天轻量读取，再按 `business_date` 二次过滤，以保留跨午夜续跑；命中 1000 条读取上限时返回 `truncated=true` 并丢弃边界业务日的可能不完整日组。

### GET action targets

返回 `raw_result_count`、`samples`、`results`、`audit`。只有用户展开某个批次时调用；每个批次详情只发一次请求。daily 日卡不会提前拼接全天大 JSON。

### POST preview-live

请求体可为空，路径中的 id 写入 `rule_group_id`。响应保留原字段，并新增：

```json
{
  "scan_count": 1940,
  "candidate_count": 927,
  "pause_count": 927,
  "execution_count": 200,
  "execution_remaining_count": 727
}
```

`execution_count` 是本批计划，不是规则命中总量。

### POST execute-live

```json
{
  "preview_id": "...",
  "preview_hash": "...",
  "dry_run": false,
  "confirm": "EXECUTE_LIVE_PAUSE"
}
```

响应新增：`run_status`、`deferred_count`、`remaining_count`、`retryable_error_count`、`permanent_error_count`、`blocked_count`、`preview_error_count`、`log_store`、`log_store_error`。

## 错误码

| code/reason | 含义 | 处理 |
| --- | --- | --- |
| `preview_hash_mismatch` | preview与确认不一致 | 拒绝执行，重新preview |
| `confirm_required` | 正式暂停缺少显式确认 | 拒绝执行 |
| Graph `4/5044001` | 应用请求上限 | 当前真实失败1条，未发请求deferred，后续续跑 |
| `missing_meta_token` | 产品默认user无可用token | blocked，不继续写Meta |
| `outside_product_whitelist` | 当前Campaign已不在产品白名单 | blocked/fail closed |
| `account_owner_mismatch` | owner缺失或不匹配 | blocked/fail closed |
| `not_active` | 当前已非ACTIVE | 终态跳过 |
| `sqlite_fallback` | ads_ai暂时不可用 | 不改变Meta计数，日志先留SQLite |

## 兼容性说明

- 保留原 action/preview 主键、counts 和 `/targets` 结构。
- 历史 `log_version=1` 缺少的 scan/candidate 在前端显示 `--`，不会伪装为 0。
- API 列表优先 `ads_ai`，仍合并 SQLite 中未同步 action；详情先查 MySQL，失败时回退 SQLite。
- `storage=ads_ai` 表示数据库查询健康，即使当前筛选结果为空也不会误报 fallback。
