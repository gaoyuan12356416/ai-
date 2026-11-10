# 账户池数据查询接口 V1.5

## 地址与权限

`GET https://ai.yingliangads.com/api/v1/dramawave/w2a/2334/daily-stats`

接口地址保持兼容，其中 `dramawave` 和 `2334` 是历史路径标识，**不限制产品或页面 ID**。

请求头：`Authorization: Bearer <API_TOKEN>`。Token 通过独立凭据文件交付，不写入本公开对接文档。

调用方出口 IP 必须同时属于以下网段之一：

- `91.203.98.0/24`
- `82.145.216.0/24`
- `43.166.187.96/32`

接口使用 HTTPS，服务端根据真实客户端 IP 校验，不接受调用方自行声明的转发 IP。

## 固定数据范围

固定账户池为 **648（Account for opera）**，日期下限为 **2026-11-01**。**不按产品或页面筛选**，包含池内账户的全部产品、全部页面数据。

- 展示、点击、花费来自 `kunlunads_dev.ads_tiktok_insights`，按 `advertiser_id` 匹配账户池，日期取 `start_date`，使用 Campaign 层 `category=0`。不叠加广告组、广告层，避免重复计数。
- 安装、收入继续来自 `kunlunads_dev.ads_custom_source_insight`，按 `ad_account_id` 匹配同一账户池，日期取 `dt`，收入仍为 `af_revenue0`。
- 两张表分别按日汇总后合并对应指标，不关联原始明细放大数据。不因某账户仅在其中一张表有数据或花费为零而排除其指标。

账户池成员取业务账户池 `ads_alternative_account_group` 与 `ads_alternative_accounts`，排除已移出池的成员（`deleted_at` 非零），不按账户当前投放状态另行剔除其历史数据。成员 ID 去重后筛选，不通过重复成员 JOIN 放大指标。

| 返回字段 | JSON 类型 | 口径 |
|---|---|---|
| `day` | string | TikTok 表 `start_date` / 原表 `dt`，`YYYY-MM-DD` |
| `impression` | integer | TikTok 表 `SUM(show_cnt)` |
| `click` | integer | TikTok 表 `SUM(click_cnt)` |
| `install` | integer | 原表 `SUM(installs)` |
| `revenue` | number | 原表 **`SUM(af_revenue0)`**，两位小数 |
| `cost` | number | TikTok 表 `SUM(stat_cost)`，两位小数 |

日期和金额沿用源表口径，不重新切日或换算汇率。`revenue` 不代表按收入事件日另行重算的实际收款。

ROAS 可由调用方按 `revenue / cost × 100%` 计算；花费为零时 ROAS 不适用。接口字段保持不变，不新增 ROAS 字段。

## 查询参数与示例

| 参数 | 必填 | 说明 |
|---|---|---|
| `start_date` | 是 | 起始日期，`YYYY-MM-DD` |
| `end_date` | 是 | 截止日期，`YYYY-MM-DD`，不得早于起始日期 |

包含起止两天，每天最多一行，按日期升序。只接受上述两个日期参数，禁止重复参数；账户池由服务端固定，不接受产品或页面筛选参数。

**数据起始日为 2026-11-01。** 查询完全早于该日时，成功返回 `data: []`；跨越该日时，只返回 2026-11-01 及之后的数据。该下限同时应用于源表读取与接口输出，调用方不能覆盖。

```bash
curl --get \
  'https://ai.yingliangads.com/api/v1/dramawave/w2a/2334/daily-stats' \
  --header 'Authorization: Bearer <API_TOKEN>' \
  --data-urlencode 'start_date=2026-11-01' \
  --data-urlencode 'end_date=2026-11-06'
```

成功响应（模拟数据，仅说明格式）：

```json
{
  "code": "OK",
  "message": "success",
  "updated_at": "2026-11-06T10:00:00+08:00",
  "stale": false,
  "data": [
    {
      "day": "2026-11-06",
      "impression": 10000,
      "click": 320,
      "install": 45,
      "revenue": 128.50,
      "cost": 96.20
    }
  ]
}
```

`updated_at` 是最后一次成功生成接口快照的时间（包含时区），不是源表数据发生时间。查询成功但无匹配行，或账户池合法存在且为空时，返回 HTTP 200 和 `data: []`，不补造零值记录。

## 更新规则

- 后台每小时整点读取一次，在同一只读查询快照中汇总两张表的全部已有日期，完成后整体替换快照。HTTP 请求只读取已生成快照。V1.5 首次成功刷新会按新数据源回溯 11 月 1 日以来的数据；当天数值为截至刷新时的未完结数据。
- 每行是该日期的最新汇总值。对接方应按 `day` 覆盖更新，不能累加不同小时的返回值。源表历史修订会反映到后续快照。
- 每次刷新读取当前账户池成员；成员变化会重新计算历史数据。刷新过程中再次核对成员，成员范围变化时旧范围快照不继续开放。
- 源指标查询失败时，保留同范围上一份完整快照和原 `updated_at`，并返回 `stale: true`。没有成功快照时返回 503。
- 账户池读取失败或不能确定新范围时，不使用旧成员名单放行。配置未完成不退化成全账户读取或全网访问。

## 状态码

| HTTP | code | 含义 |
|---|---|---|
| 200 | `OK` | 查询成功，包括空数据 |
| 400 | `INVALID_PARAMETER` | 日期缺失、格式错误、范围错误、重复或额外参数 |
| 401 | `UNAUTHORIZED` | Token 缺失或无效 |
| 403 | `IP_NOT_ALLOWED` | 出口 IP 未获允许 |
| 503 | `CONFIG_NOT_READY` | 必要配置或账户池范围尚不可用 |
| 503 | `DATA_NOT_READY` | 没有符合当前范围的可用快照 |

错误返回格式：

```json
{"code":"CONFIG_NOT_READY","message":"账户池尚未配置"}
```

当 Token 和 IP 同时不符合要求时，优先返回 401。重复调用 GET 不会创建业务记录。
