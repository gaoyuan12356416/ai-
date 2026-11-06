# DramaWave W2A 数据查询接口 V1.1

## 地址与权限

`GET https://ai.yingliangads.com/api/v1/dramawave/w2a/2334/daily-stats`

请求头：`Authorization: Bearer <API_TOKEN>`。Token 通过独立凭据文件交付，不写入本公开对接文档。

调用方出口 IP 必须同时属于以下网段之一：

- `91.203.98.0/24`
- `82.145.216.0/24`

接口使用 HTTPS，服务端根据真实客户端 IP 校验，不接受调用方自行声明的转发 IP。

## 固定数据范围

数据源为 `kunlunads_dev.ads_custom_source_insight`。固定筛选 `product='Dramawave'`、`w2a_page_id=2334`，并限定 `ad_account_id` 属于账户池 **648（Account for opera）**。五项指标使用同一范围。

账户池成员取业务账户池 `ads_alternative_account_group` 与 `ads_alternative_accounts`，排除已移出池的成员（`deleted_at` 非零），不按账户当前投放状态另行剔除其历史数据。成员 ID 去重后筛选，不通过重复成员 JOIN 放大指标。

| 返回字段 | JSON 类型 | 口径 |
|---|---|---|
| `day` | string | 源表 `dt`，`YYYY-MM-DD` |
| `impression` | integer | `SUM(impressions)` |
| `click` | integer | `SUM(clicks)` |
| `install` | integer | `SUM(installs)` |
| `revenue` | number | **`SUM(af_revenue0)`**，两位小数 |
| `cost` | number | `SUM(spend)`，两位小数 |

日期和金额沿用源表口径，不重新切日或换算汇率。`revenue` 不代表按收入事件日另行重算的实际收款。

## 查询参数与示例

| 参数 | 必填 | 说明 |
|---|---|---|
| `start_date` | 是 | 起始日期，`YYYY-MM-DD` |
| `end_date` | 是 | 截止日期，`YYYY-MM-DD`，不得早于起始日期 |

包含起止两天，每天最多一行，按日期升序。只接受上述两个参数，禁止重复参数；不能通过参数更换产品、页面或账户池。

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

- 后台每小时整点读取一次，重新汇总该范围内全部已有日期，完成后整体替换快照。HTTP 请求只读取已生成快照。
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
