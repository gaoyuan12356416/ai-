# TT 自动发布模板 API

## 通用约束

- 前缀：`/api/admin/tt-auto-publish`
- 权限：后台 Cookie + `tt_posts`，并由具体导航项控制。
- 写请求：`Content-Type: application/json` 且通过 same-origin 校验。
- 响应：`{"ok": true, ...}` 或 `{"ok": false, "error": "error_code", "code": "error_code", "message": "脱敏说明"}`。
- 所有时间均返回 ISO 8601；计划输入为北京时间 `HH:mm`。
- 浏览器响应不返回 token、源素材 URL、GPU 准备后 URL或黑名单值明细。运行只返回黑名单加载时间、行数和 SHA256；任务只返回 `prepared` 布尔值。`publish_url` 仅在它是无凭据、无查询/片段的受信 TikTok HTTPS URL 时返回。

## 账号

### `GET /accounts`

返回可选 TT 账号，仅包含账号 ID、显示名、剧语言、账号设置可用状态和资格摘要，不返回 token。

## 模板

### `GET /templates`

查询参数：`status`、`q`、`limit`、`offset`。返回模板、当前版本、账号数、计划摘要和最近运行。

### `POST /templates`

创建停用模板。主体核心字段：

```json
{
  "name": "EN spend first",
  "account_ids": ["tt-account-id"],
  "caption_template": "{desc}\n{url}",
  "metric_window_days": 7,
  "drama_launch_window_days": 0,
  "cooldown_days": 0,
  "platform": 0,
  "drama_rule": {
    "resource_type_v2": ["1"],
    "spend_min": "0",
    "spend_max": null,
    "roas_min": null,
    "roas_max": null,
    "sort_by": "spend",
    "sort_direction": "desc"
  },
  "material_rule": {
    "duration_min_seconds": 1,
    "duration_max_seconds": 180,
    "spend_min": "0",
    "spend_max": null,
    "roas_min": null,
    "roas_max": null,
    "sort_by": "roas",
    "sort_direction": "desc"
  },
  "schedule": {"mode": "fixed", "times": ["10:00", "18:00"]}
}
```

`caption_template` 必填但不强制包含剧 ID 宏。`{{content_id}}` 与
`{{contect_id}}` 均为可选兼容宏；模板可仅使用 `{desc}`、`{url}`，也可使用
不含任何宏的固定文案。空模板、未知或不完整宏、超过长度限制的文案仍会被拒绝；
自动发布模板继续不支持 `{code}`。

`drama_rule.resource_type_v2` 可省略或传空数组，表示不限制短剧类型。非空时仅允许生产字段备注中的 `0`、`1`–`22`、`100`；响应统一为字符串数组。中文含义如下：

| 值 | 中文选项 | 值 | 中文选项 |
| --- | --- | --- | --- |
| 0 | 其他 | 1 | 翻译剧非首发 |
| 2 | 本土首发 | 3 | 本土对投 |
| 4 | 本土二轮采买 | 5 | 本土自制 |
| 6 | 翻译剧首发 | 7 | 首发本土动态漫 |
| 8 | 二轮本土动态漫 | 9 | 首发翻译动态漫 |
| 10 | 二轮翻译动态漫 | 11 | 翻译剧自制 |
| 12 | 漫剧自制 | 13 | AI本土真人剧自制 |
| 14 | AI本土真人剧首发 | 15 | 二轮本土AI真人剧 |
| 16 | 翻译AI真人剧首发 | 17 | 二轮翻译AI真人剧 |
| 18 | AI本土解说剧自制 | 19 | AI本土解说剧首发 |
| 20 | AI本土解说剧二轮 | 21 | AI翻译解说剧首发 |
| 22 | AI翻译解说剧首发 | 100 | 小说 |

`-1`（所有类型）不作为筛选值；“所有类型”由空数组表达，避免生成对 `resource_type_v2=-1` 的错误精确过滤。

### `GET /templates/{id}`

返回模板当前配置、版本、审计字段、`enabled_at_utc` 和最近运行摘要。`enabled_at_utc` 是本次从停用切到启用的时刻；同状态重复启用不会移动该时间。

### `POST /templates/{id}`

编辑并生成新版本。必须包含 `expected_version`；冲突返回 409。

### `POST /templates/{id}/copy`

复制当前版本，新模板默认停用。可传新名称。

### `POST /templates/{id}/enable` / `disable`

必须包含 `expected_version`。启用前验证账号设置、规则和计划；停用不取消已有任务。自动调度建 run 时仍会在同一事务内复核当前启用状态、版本、计划时刻不早于 `enabled_at_utc`，因此陈旧调度快照不能在停用、编辑或刚启用后补建旧 run。

### `POST /templates/{id}/preview`

按账号返回候选或拒绝原因。仅读取，不冻结素材、不创建 run/task。

### `POST /templates/{id}/run-now`

```json
{
  "expected_version": 3,
  "confirmed": true,
  "idempotency_key": "tt-auto-run-20260805T120000Z-abcd1234"
}
```

即使模板停用也可执行。返回 202 和 `run_id`；每账号异步执行一次。调用方必须为一次点击生成 8 至 128 字符的稳定幂等键，并在网络未知或 5xx 重试时复用；同一模板版本和幂等键返回同一 run 且 `idempotent=true`。幂等键在新系统内全局唯一，同一键若被另一个模板或版本复用则返回 409。前端只可在确定成功或确定 4xx 后清理该键。

## 运行

### `GET /runs`

查询参数：`template_id`、`trigger_type`、`status`、`from`、`to`、`limit`、`offset`。`from`/`to` 是北京时间日期，服务端转换为 `[from 00:00, to+1d 00:00)` 的 UTC 半开区间。

### `GET /runs/{id}`

返回 run、模板版本快照、账号任务、冻结候选指标、事件、prepare/publish/reconcile 事实；敏感凭据与内部媒体 URL始终脱敏，黑名单仅返回摘要。

## 内部 runner

内部接口仅监听 loopback并要求独立 bearer token；该 token 必须区别于旧 TT 内部 token 和 GPU token：

- `POST /internal/tt-auto-post/tick`：空 JSON 主体；仅生成到期运行，不执行耗时任务。
- `POST /internal/tt-auto-post/execute-next`：主体 `{"worker_id":"..."}`；按账号串行优先级认领并执行至多一个任务。

指标刷新由独立 `tt_auto_post_metric_runner.py` 直接写入新系统 SQLite generation，不通过浏览器或内部 HTTP API。scheduler 与 worker 分开运行，避免 GPU prepare 阻塞下一分钟的排期。
