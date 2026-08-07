# API 文档

> 状态（2026-08-07）：本文定义目标 D7/D10 合同，尚未切换生产。生产仍运行 2026-08-06 发布的历史 D7/D30 基线。当前 D10 最早数据日期为 `2026-08-01`，原 `2026-07-29` 起点待源侧补齐，或待用户批准改为 `2026-08-01`；边界确定前不得把下文合同示例当作生产响应证据。

## 1. 访问方式与通用约定

生产入口前缀：

```text
/reports/dramawave-attribution-comparison/
```

页面及 API 由 Nginx 复用 TT 报表的飞书登录鉴权。未登录请求会跳转到飞书登录页，无权限用户返回 403。服务本身只监听 `127.0.0.1:8832`，普通页面/API 请求仅以 SQLite 只读模式打开缓存，不连接 MySQL。

接口只支持 `GET` 和 `HEAD`。除 CSV 外，成功和失败响应均为 UTF-8 JSON。所有 ID 均以字符串返回，前端不得转为 JavaScript `Number`。

JSON 响应支持：

- `ETag`；请求携带相同的 `If-None-Match` 时返回 304。
- 响应正文不少于 1024 字节且请求声明接受 gzip 时，服务返回 gzip。
- `/api/meta`、`/healthz` 和错误响应使用 `Cache-Control: private, no-store`。
- `/api/options`、`/api/query` 和 `/api/export.csv` 使用 `Cache-Control: private, max-age=0, must-revalidate`。

下文示例使用服务内部路径。经 Nginx 访问时在路径前增加 `/reports/dramawave-attribution-comparison`，例如：

```text
/reports/dramawave-attribution-comparison/api/query
```

## 2. data_version 一致性

`data_version` 是一次完整缓存快照的版本。推荐客户端流程：

1. 调用 `/api/meta` 取得 `data_version`。
2. 校验 `api_schema_version=2`、`comparison_window=D10` 和 `new_attribution_source=kunlunads_dev.ads_app_revenues_10d`。
3. 调用 `/api/options`、`/api/query`、`/api/rankings` 和 `/api/export.csv` 时携带 `api_schema_version=2`，并原样携带 `data_version`。
4. 分页期间继续携带同一版本。
5. 如果后台刷新推进了版本，接口返回 HTTP 409；客户端重新读取 `/api/meta`，刷新筛选项并从第 1 页重新查询。

如果未传 `data_version`，接口使用请求开始时读到的当前缓存版本，但不会提供跨请求的版本冲突保护。

版本冲突响应：

```http
HTTP/1.1 409 Conflict
Content-Type: application/json; charset=utf-8
Cache-Control: private, no-store

{"error":"data_version changed; reload from page 1"}
```

## 3. 公共查询参数

以下参数用于 `/api/query` 和 `/api/export.csv`；`api_schema_version`、日期和 `data_version` 也适用于 `/api/options` 与 `/api/rankings`。

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `api_schema_version` | integer string | 无 | D10 合同固定为 `2`，用于 `/api/options`、`/api/query`、`/api/rankings` 和 `/api/export.csv`；缺失或不匹配返回 409，客户端必须重新加载页面。 |
| `start_date` | `YYYY-MM-DD` | 缓存最早日期 | 起始日期，含当天；兼容别名 `start`。不得早于 `/api/meta.minimum_date`。D10 切换时该值只能是源侧补齐后的 `2026-07-29`，或用户批准后的 `2026-08-01`。 |
| `end_date` | `YYYY-MM-DD` | 缓存最晚日期 | 结束日期，含当天；兼容别名 `end`。 |
| `data_version` | string | 空 | 可选快照版本；与当前版本不一致时返回 409。 |
| `dimensions` | comma-separated string | `dt,campaign` | 聚合维度；兼容别名 `group_by`。至少一个。 |
| `metric_basis` | `d0` 或 `d7` | `d0` | 两个归因窗口内部使用的收入累计日；兼容别名 `basis`。 |
| `sort_by` | string | `spend` | 排序字段；兼容别名 `sort`。 |
| `sort_dir` | `asc` 或 `desc` | `desc` | `/api/query` 的排序方向。 |
| `limit` | integer | `50` | `/api/query` 每页条数；限制在 1～500。非法整数按 50 处理。 |
| `offset` | integer | `0` | `/api/query` 偏移量；限制在 0～10,000,000。非法整数按 0 处理。 |
| `include_rankings` | `0` / `1` | `1` | 仅用于 `/api/query`；`0` 时跳过四组排行以优先返回 KPI/趋势/主表。现有客户端不传仍保持完整响应。 |

### 3.1 dimensions 白名单

| 值 | 返回字段 | 说明 |
| --- | --- | --- |
| `dt` | `dt` | 日期 |
| `channel` | `channel` | 渠道名称 |
| `delivery_product` | `delivery_product` | 投放产品，即缓存中的 `app_id` |
| `optimizer` | `optimizer`, `optimizer_name` | 优化师 ID 和名称 |
| `country_group` | `country_group` | 国家组 |
| `account` | `account` | 广告账户 ID |
| `campaign` | `campaign`, `campaign_name` | Campaign ID 和名称 |
| `adset` | `adset`, `adset_name` | Ad Set ID 和名称 |
| `matched_grain` | `matched_grain` | 收入映射层级 |

兼容的维度别名：

```text
date -> dt
app_id -> delivery_product
optimizer_id -> optimizer
campaign_id -> campaign
adset_id -> adset
ad_account_id -> account
mapping_level -> matched_grain
```

### 3.2 精确筛选参数

下列参数按精确值筛选，可重复传递以表达多选。当前实现不拆分单个参数值中的逗号。

| 参数 | SQLite 字段 | 示例 |
| --- | --- | --- |
| `channel` | `channel_id` | `channel=0&channel=3` |
| `app_id` | `app_id` | `app_id=Dramawave` |
| `optimizer_id` | `optimizer_id` | `optimizer_id=123` |
| `country_group` | `country_group` | `country_group=US` |
| `account_id` | `ad_account_id` | `account_id=9000000000000000001` |

### 3.3 Campaign / Ad Set 搜索

| 参数 | 行为 |
| --- | --- |
| `campaign` | 在 `campaign_id` 和 `campaign_name` 中进行转义后的包含搜索；兼容别名 `campaign_q`。 |
| `adset` | 在 `adset_id` 和 `adset_name` 中进行转义后的包含搜索；兼容别名 `adset_q`。 |

`%`、`_` 和反斜杠会被转义，不作为 SQL 通配符执行。

### 3.4 sort_by 白名单

可按当前已选中的任一维度排序，也可按以下指标排序：

```text
spend, impressions, clicks, installs, af_installs,
d7_revenue, d10_revenue, d7_roas, d10_roas,
revenue_diff, lift_rate
```

## 4. GET /healthz

检查 SQLite 缓存是否可读及是否存在事实数据。

成功响应形状示例（字段值仅用于合同说明，不是 D10 生产验收数据；示例暂按当前已存在的 `2026-08-01` 表达）：

```json
{
  "ok": true,
  "stale": false,
  "data_version": "EXAMPLE_D10_VERSION",
  "generated_at": "YYYY-MM-DDTHH:MM:SS+08:00",
  "cache_start_date": "2026-08-01",
  "cache_end_date": "2026-08-01",
  "fact_rows": 18420,
  "cache_complete": true,
  "current_date_present": true,
  "missing_dates": [],
  "rollups_current": true
}
```

`generated_at` 距当前时间超过 45 分钟时 `stale=true`。只有事实行和 `data_version` 均非空、日级汇总与事实版本一致、并且从保留起点到缓存终点之间没有日期空洞时 HTTP 状态才为 200；事实表为空、版本为空、汇总版本落后、日期不连续或缓存不可用时返回 503。`stale` 与结构完整性分开报告，不会把过期缓存伪装成日期缺失。

## 5. GET /api/meta

返回缓存边界、当前版本、默认查询条件和可用维度。

无查询参数。

响应形状示例（占位时间/版本不是 D10 实测数据；边界最终值仍以业务决策和真实缓存为准）：

```json
{
  "api_schema_version": 2,
  "data_version": "EXAMPLE_D10_VERSION",
  "comparison_window": "D10",
  "new_attribution_source": "kunlunads_dev.ads_app_revenues_10d",
  "generated_at": "YYYY-MM-DDTHH:MM:SS+08:00",
  "source_max_updated_at": {
    "ads_custom_source_insight": "YYYY-MM-DD HH:MM:SS",
    "app_revenues": "YYYY-MM-DD HH:MM:SS",
    "app_revenues_10d": "YYYY-MM-DD HH:MM:SS"
  },
  "cache": {
    "start_date": "2026-08-01",
    "end_date": "2026-08-01",
    "retention_days": 60,
    "refresh_interval_minutes": 30,
    "stale": false,
    "complete": true,
    "range_complete": true,
    "current_date_present": true,
    "expected_start_date": "2026-08-01",
    "missing_dates": [],
    "rollups_current": true
  },
  "defaults": {
    "start_date": "2026-08-01",
    "end_date": "2026-08-01",
    "basis": "d0",
    "dimensions": ["dt", "campaign"]
  },
  "dimensions": [
    "dt", "channel", "delivery_product", "optimizer", "country_group",
    "account", "campaign", "adset", "matched_grain"
  ],
  "metric_bases": ["d0", "d7"],
  "minimum_date": "2026-08-01",
  "source_tables": {
    "old_attribution": "kunlunads_dev.ads_app_revenues",
    "new_attribution": "kunlunads_dev.ads_app_revenues_10d",
    "delivery": "kunlunads_dev.ads_custom_source_insight"
  }
}
```

`defaults.start_date` 是缓存范围内最近 7 天的起点。它只是页面建议值；其他接口在未传日期时仍默认使用整个缓存可用范围。上例以“批准起点改为 8 月 1 日”的候选分支说明字段形状；如果源侧补齐并保留原边界，`minimum_date` / `expected_start_date` 应为 `2026-07-29`。

`complete` / `range_complete` 表示 `expected_start_date` 到 `cache.end_date` 每个自然日都有成功缓存；`current_date_present` 单独说明北京当天是否已进入缓存。页面会显式警告日期空洞或当天尚未完成首轮刷新。任一查询的请求区间包含缺失日期时返回 503，而不是把缺失日静默显示为 0。

## 6. GET /api/options

返回指定日期范围内的低基数筛选项。

支持参数：必填 `api_schema_version=2`，以及 `start_date`、`end_date`、`data_version` 和日期别名。当前接口不应用其他业务筛选条件，也不返回 Campaign/Ad Set 选项。

请求示例：

```text
GET /api/options?api_schema_version=2&start_date=2026-08-01&end_date=2026-08-01&data_version=EXAMPLE_D10_VERSION
```

响应示例：

```json
{
  "data_version": "EXAMPLE_D10_VERSION",
  "options": {
    "channel": [
      {"value": "0", "label": "Meta"},
      {"value": "3", "label": "TikTok"}
    ],
    "app_id": [
      {"value": "Dramawave", "label": "Dramawave"}
    ],
    "optimizer_id": [
      {"value": "123", "label": "Alice"}
    ],
    "country_group": [
      {"value": "US", "label": "US"}
    ],
    "ad_account_id": [
      {"value": "9000000000000000001", "label": "9000000000000000001"}
    ]
  }
}
```

每个选项均为 `{value, label}`。空值不会返回。

## 7. GET /api/query

按白名单筛选和维度执行服务端聚合，一次返回 KPI、日趋势、四组排行和当前页结果。

请求示例：

```text
GET /api/query?api_schema_version=2&start_date=2026-08-01&end_date=2026-08-01&dimensions=dt,campaign&metric_basis=d0&channel=0&country_group=US&sort_by=spend&sort_dir=desc&limit=50&offset=0&data_version=EXAMPLE_D10_VERSION
```

响应结构（以下金额和计数沿用合成 fixture 只说明字段关系，不是 D10 源表点样本或生产验收值）：

```json
{
  "data_version": "EXAMPLE_D10_VERSION",
  "generated_at": "YYYY-MM-DDTHH:MM:SS+08:00",
  "metric_basis": "d0",
  "dimensions": ["dt", "campaign"],
  "totals": {
    "spend": 200.0,
    "impressions": 2000,
    "clicks": 180,
    "installs": 40,
    "af_installs": 36,
    "d7_revenue": 60.0,
    "d10_revenue": 95.0,
    "d7_roas": 0.3,
    "d10_roas": 0.475,
    "revenue_diff": 35.0,
    "lift_rate": 0.5833333333,
    "d7_users": 35,
    "d7_purchases": 5,
    "d10_users": 37,
    "d10_purchases": 7
  },
  "trend": [
    {
      "dt": "2026-08-01",
      "spend": 200.0,
      "d7_revenue": 60.0,
      "d10_revenue": 95.0,
      "d7_roas": 0.3,
      "d10_roas": 0.475,
      "revenue_diff": 35.0,
      "lift_rate": 0.5833333333
    }
  ],
  "rankings": {
    "campaign": [],
    "adset": [],
    "optimizer": [],
    "country_group": []
  },
  "rows": [
    {
      "dt": "2026-08-01",
      "campaign": "1200000000000000001",
      "campaign_name": "Campaign A",
      "spend": 100.0,
      "d7_revenue": 50.0,
      "d10_revenue": 75.0,
      "d7_roas": 0.5,
      "d10_roas": 0.75,
      "revenue_diff": 25.0,
      "lift_rate": 0.5,
      "d7_users": 18,
      "d7_purchases": 2,
      "d10_users": 19,
      "d10_purchases": 3
    }
  ],
  "pagination": {
    "total": 2,
    "offset": 0,
    "returned": 1,
    "limit": 50
  },
  "mapping_quality": {}
}
```

### 7.1 指标语义

- `spend` 只汇总一次 `ads_custom_source_insight.spend`，是 D7/D10 两个 ROAS 的共同分母。
- `metric_basis=d0` 时，`d7_revenue` 和 `d10_revenue` 分别汇总对应归因表的 `revenue_iaa_d0 + revenue_iap_d0`。
- `metric_basis=d7` 时，分别汇总 `revenue_iaa_d7 + revenue_iap_d7`。
- `d7_roas = d7_revenue / spend`；`d10_roas = d10_revenue / spend`。
- `revenue_diff = d10_revenue - d7_revenue`。
- `lift_rate = revenue_diff / d7_revenue`。
- `d7_purchases`、`d10_purchases` 随 `metric_basis` 切换 D0/D7 purchase 字段。
- `users` 在源表中没有 D0/D7 后缀，因此 `d7_users`、`d10_users` 不随 `metric_basis` 切换。
- 所有比率均在筛选和分组汇总后重新相除，不平均行级比率。
- `spend=0` 时两个 ROAS 为 `null`；旧口径收入为 0 时 `lift_rate=null`。

`totals`、`trend`、`rankings` 和 `rows` 的指标字段相同。`trend` 固定按 `dt` 升序；`rankings` 固定包含 `campaign`、`adset`、`optimizer` 和 `country_group`，各按 spend 降序返回最多 8 条。排行榜不受主表分页影响。

服务端会按请求选择最小且语义完整的缓存层：不含 Campaign/Ad Set 搜索时 totals/trend 与筛选维度使用日级筛选汇总；Campaign 维度/搜索使用 Campaign 日级汇总；Ad Set 维度/搜索回退最细事实。所有层与事实在同一事务和同一 `data_version` 内更新，并在刷新时做可加指标守恒。调用方不需要、也不能指定物理表名。

### 7.2 mapping_quality 字段

```text
d7_total_rows, d7_mapped_rows, d7_ambiguous_rows, d7_unmapped_rows,
d7_total_revenue, d7_mapped_revenue,
d7_coverage_ratio, d7_revenue_coverage_ratio,
d10_total_rows, d10_mapped_rows, d10_ambiguous_rows, d10_unmapped_rows,
d10_total_revenue, d10_mapped_revenue,
d10_coverage_ratio, d10_revenue_coverage_ratio,
total_revenue_rows, mapped_revenue_rows, ambiguous_revenue_rows,
unmapped_revenue_rows, mapped_ratio, source_scope_exclusions
```

其中：

- `*_coverage_ratio = mapped_rows / total_rows`。
- `*_revenue_coverage_ratio = mapped_revenue / total_revenue`。
- 分母为 0 时对应覆盖率为 `null`。
- 覆盖率分母仅包含至少命中一条 `product='Dramawave'` custom 维度候选的 revenue 键。两张 revenue 表本身没有产品字段，完全未命中的全表行无法可靠判断产品归属，因此不进入看板；刷新日志会分别记录源表总金额、候选金额和被排除的未定产品范围金额，以证明全表金额守恒。
- `source_scope_exclusions` 是对象，固定包含 `scope="date_range_global_not_filter_attributable"`、`business_filters_applied`、`d7_rows`、`d10_rows`、`d7_revenue`、`d10_revenue`。它从请求日期内每个自然日最新一条成功刷新审计聚合，表示未命中任何 Dramawave custom 候选、因而无法判定产品归属的全源键。它只具有日期级含义；当请求还带渠道、国家、优化师等业务筛选时，`business_filters_applied=true`，这些数量/金额仍不得解释为筛选后的排除量。
- `*_unmapped_rows` 只表示已经进入 Dramawave 候选分母后的未匹配数，不包含 `source_scope_exclusions`；UI 显示为“候选内未匹配”。
- 不带 D7/D10 前缀的五个字段是兼容旧客户端的合并字段；其中计数取两个口径对应计数的较大值，`mapped_ratio` 取两个非空覆盖率中的较小值。

### 7.3 GET /api/rankings

使用与 `/api/query` 相同的必填 `api_schema_version=2`、日期、业务筛选、`metric_basis` 和 `data_version`，独立返回四组排行：

```json
{
  "data_version": "EXAMPLE_D10_VERSION",
  "rankings": {
    "campaign": [],
    "adset": [],
    "optimizer": [],
    "country_group": []
  }
}
```

该接口不接受分页、主表排序或主表分组作为缓存语义；页面先以 `/api/query?include_rankings=0` 渲染 KPI、趋势和表格，再异步加载排行。排行失败只影响排行卡片，不会把已经成功的主表标记为当前筛选失败。

## 8. GET /api/export.csv

使用与 `/api/query` 相同的日期、筛选、维度、指标基准和排序规则导出完整聚合结果，不应用 `limit` 和 `offset`。

请求示例：

```text
GET /api/export.csv?api_schema_version=2&start_date=2026-08-01&end_date=2026-08-01&dimensions=campaign,adset&metric_basis=d7&channel=0&sort_by=d10_revenue&sort_dir=desc&data_version=EXAMPLE_D10_VERSION
```

成功响应：

```http
HTTP/1.1 200 OK
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename="dramawave-attribution-2026-08-01-2026-08-01.csv"
```

CSV 使用 UTF-8 BOM。存在数据时列名与聚合行实际字段一致，包含所选维度、维度名称和全部聚合指标。导出聚合行超过 200,000 时返回 413，调用方需要缩小日期或筛选范围。

## 9. 错误码

错误 JSON 的基本结构为：

```json
{"error":"错误说明"}
```

缓存错误可能额外包含 `detail`。

| HTTP 状态 | 触发条件 | 典型响应 |
| --- | --- | --- |
| 302 | 经 Nginx 访问但未登录飞书 | 跳转 `/api/auth/feishu/login?next=/reports/dramawave-attribution-comparison/` |
| 304 | `If-None-Match` 与当前 200 响应的 ETag 一致 | 无正文 |
| 400 | 日期格式非法、早于 `/api/meta.minimum_date`、起始日期晚于结束日期、日期超出当前缓存边界、不支持的维度/排序字段、非法 `metric_basis`、`/api/query` 非法 `sort_dir` | `{"error":"..."}` |
| 403 | 飞书用户不在允许范围；或 Nginx 拒绝 API 非 GET 方法 | HTML 403 页面或 Nginx 默认 403 |
| 404 | 未定义路径 | `{"error":"not found"}` |
| 409 | `api_schema_version` 缺失/不等于 `2`，或请求携带的 `data_version` 不等于当前缓存版本 | `{"error":"api_schema_version=2 is required; reload the dashboard"}` 或 `{"error":"data_version changed; reload from page 1"}` |
| 413 | CSV 聚合结果超过 200,000 行 | `{"error":"export contains N rows; narrow filters below 200000"}` |
| 500 | 未被参数校验包装的异常 | `{"error":"internal server error"}` |
| 503 | SQLite 文件不存在/不可读、SQLite 查询错误、缓存为空；`/healthz` 事实表为空 | `{"error":"cache unavailable","detail":"..."}` 或 health 响应 |

## 10. 完整调用示例

```javascript
async function loadPage() {
  const prefix = '/reports/dramawave-attribution-comparison';
  const meta = await fetch(`${prefix}/api/meta`, {credentials: 'same-origin'}).then(r => r.json());

  const params = new URLSearchParams({
    api_schema_version: String(meta.api_schema_version),
    start_date: meta.defaults.start_date,
    end_date: meta.defaults.end_date,
    dimensions: 'dt,campaign',
    metric_basis: 'd0',
    sort_by: 'spend',
    sort_dir: 'desc',
    limit: '50',
    offset: '0',
    data_version: meta.data_version
  });
  params.append('channel', '0');
  params.append('channel', '3');

  const response = await fetch(`${prefix}/api/query?${params}`, {credentials: 'same-origin'});
  if (response.status === 409) {
    // 重新读取 meta/options，并从 offset=0 开始。
    return loadPage();
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
```

## 11. 变更记录

- 2026-08-07：目标新口径由 D30 改为 D10；所有 API、排序、质量和导出字段从 `d30_*` 改为 `d10_*`，新源表改为 `kunlunads_dev.ads_app_revenues_10d`。D10 本地自动化 `62/62` 通过；示例仍只说明目标合同，生产继续使用历史 D30 基线，待日期边界决策和候选/生产重新验收后切换。
