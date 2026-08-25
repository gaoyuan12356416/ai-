# API 文档

## 接口列表

## 请求/响应

## 错误码

## 兼容性说明
# AI 游戏报表静态接口契约

## 鉴权

所有路径通过 Nginx `auth_request /_tt_minis_report_auth` 复用现有飞书租户鉴权。匿名请求跳转 `/api/auth/feishu/login?next=/reports/ai-game-performance/`；未授权租户返回 403。响应必须带 `Vary: Cookie`，禁止共享缓存。

## 页面

### `GET /reports/ai-game-performance/`

- 内容：`index.html`；
- 缓存：`Cache-Control: private, no-store`；
- 页面本身不包含业务数据，也不发起 MySQL 请求。

## 当前清单

### `GET /reports/ai-game-performance/latest.json`

- 缓存：`Cache-Control: private, no-store`；
- 每次成功发布最后原子替换，是公开版本的提交点。

主要结构：

```json
{
  "meta": {
    "title": "AI游戏产品测转化报表",
    "generated_at": "2026-08-25 16:00:00",
    "data_version": "20260825T160000123456+0800",
    "start_date": "2026-08-10",
    "end_date": "2026-08-25",
    "default_date": "2026-08-24",
    "timezone": "Asia/Shanghai",
    "source_note": "...",
    "currency_note": "...",
    "today_partial": true
  },
  "views": {
    "overview": {"label": "游戏总览"},
    "delivery": {"label": "渠道明细"},
    "conversion": {"label": "转化明细"}
  },
  "data_files": {
    "overview": {"2026-08-24": "data/<version>/overview/2026-08-24.json"},
    "delivery": {"2026-08-24": "data/<version>/delivery/2026-08-24.json"},
    "conversion": {"2026-08-24": "data/<version>/conversion/2026-08-24.json"}
  },
  "row_counts": {},
  "summary": {},
  "daily_totals": {},
  "quality": {}
}
```

`quality` 必含 `source_rows/source_spend/mapped_rows/mapped_spend/mapped_row_ratio/mapped_spend_ratio/ambiguous_rows/ambiguous_spend/unmapped_rows/unmapped_spend/manual_rows/manual_installs/manual_cost`。

## 版本化日文件

### `GET /reports/ai-game-performance/data/<version>/<view>/<YYYY-MM-DD>.json`

- `<view>`：`overview`、`delivery` 或 `conversion`；
- 缓存：`Cache-Control: private, max-age=900`；
- URL 由 `latest.json` 返回，客户端不得自行猜版本；
- 数据使用字典编码：

```json
{
  "view": "overview",
  "date": "2026-08-24",
  "data_version": "...",
  "columns": ["dt", "game_id", "game_name"],
  "dict_columns": ["dt", "game_id", "game_name"],
  "dicts": {"dt": ["2026-08-24"]},
  "rows": [[0, 0, 0]]
}
```

当列名出现在 `dict_columns` 中时，行值是 `dicts[column]` 的下标；其他列为原始数值。

## 视图字段

### 游戏总览 `overview`

共享维度：日期、游戏 ID/名称、渠道、映射状态、花费来源。

基础可加指标：`effective_spend/source_spend/source_installs/source_impressions/source_clicks/manual_cost/manual_installs/d1_retained/play_weighted_seconds/play_weight_installs/day0_revenue/day1_revenue/source_row_count/manual_row_count`。

比率必须在筛选/分组后重算：`source_ctr/source_cpi/d1_retention_rate/avg_play_duration_seconds/d0_roas/cost_per_d1_retained`。

### 渠道明细 `delivery`

维度增加 `source_country/campaign_id/adset_id/ad_id`；指标为渠道花费、安装、曝光、点击和行数。`source_country` 是渠道国家/分组。

### 转化明细 `conversion`

维度增加 `conversion_country/campaign_id/adset_id/ad_id/campaign_name/adset_name/ad_name`；指标为手工成本、测转安装、D1 留存、游戏时长权重、D0/D1 收入和行数。`conversion_country` 是转化国家。

## 错误契约

- 未登录：302 到飞书登录；
- 未授权：403；
- 日文件不存在：404；
- 客户端收到非 JSON 清单时显示中文登录失效/格式错误，不解析为业务数据；
- 刷新/发布失败不会生成公开错误 JSON，而是继续保留上一 `latest.json`。
