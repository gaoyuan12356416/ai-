# API 文档

## 接口列表
- `GET /api/ad-control/products`
  - 获取可配置产品列表。
- `GET /api/ad-control/accounts?product=...`
  - 按产品获取业务库实际账户，账户池页用于多选和 +8 筛选。
- `GET/POST/PUT/DELETE /api/ad-control/account-groups`
  - 账户池 CRUD。
- `GET/POST/PUT/DELETE /api/ad-control/rule-sets`
  - 可复用规则集 CRUD。
- `GET/POST/PUT/DELETE /api/ad-control/bindings`
  - 绑定关系 CRUD，绑定产品、账户池、规则集和策略。
- `POST /api/ad-control/bindings/{id}/enabled`
  - 启用/停用绑定。
- `POST /api/ad-control/bindings/{id}/preview-live`
  - 按绑定做实时 preview，不调用 Meta 写接口。
- `POST /api/ad-control/bindings/{id}/execute-live`
  - 按绑定真实执行，必须携带 preview hash 和确认口令。
- `POST /api/ad-control/campaign-start/refresh`
  - 刷新 campaign 起始时间缓存。
- `POST /api/ad-control/emergency-stop`
  - 急停。
- `GET /api/ad-control/actions`
  - 查询执行/preview/dry-run 审计。
- `GET /api/ad-control/runner/status`
  - 查询 runner 和资源状态。

## 请求/响应
- Binding 保存 payload 支持 `strategy`：
  - `close_time`
  - `execute_timezone`
  - `block_same_day_reopen`
  - `allow_next_day_reopen`
  - `country_groups`
  - `account_timezones`
- Rule condition 支持字段：
  - `age_hours`
  - `account_time_zone`
  - `country`
  - `spend`
  - `install`
  - `purchase`
  - `revenue`
  - `roas_pct`
  - `purchase_cpa`
  - `effective_status`
- Rule condition 支持操作符：
  - `gt`
  - `gte`
  - `lt`
  - `lte`
  - `eq`
  - `between`
  - `in`
- Preview item 补充字段：
  - `country`
  - `language`
  - `account_time_zone`
  - `strategy`

## 错误码
- `401`：未登录或登录态无效。
- `403`：无 `ad_control_center` 权限。
- `404`：绑定、规则集、账户池不存在。
- `409`：execute 的 preview hash 不匹配或未先 preview。
- `422`：payload 字段非法、规则 JSON 非法或确认口令缺失。

## 兼容性说明
- 旧 `/api/ad-control/rule-groups` 保留一版兼容，内部映射到 binding。
- 旧 `rules_json` 字段不删除；迁移后新 binding 优先使用 `rule_set_id` 指向的规则集。
- 新增 `strategy_json` 是向后兼容字段，默认 `{}`。
