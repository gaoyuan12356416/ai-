# API 文档

## 接口列表

本需求不新增内部 HTTP API，仅消费以下 TikTok API for Business v1.3 接口：

| 用途 | 方法与路径 | 官方文档 |
| --- | --- | --- |
| 查询对象当前出价保护状态 | `GET /open_api/v1.3/report/bid_protection/status/get/` | [Get bid protection statuses](https://business-api.tiktok.com/portal/docs/get-bid-protection-statuses/v1.3) |
| 查询对象按日出价保护历史及赔付金额 | `GET /open_api/v1.3/report/bid_protection/detail/get/` | [Get bid protection history](https://business-api.tiktok.com/portal/docs/get-bid-protection-history/v1.3) |

## 请求/响应

### 通用请求约束

- 鉴权通过请求头 `Access-Token` 传递；任何日志、示例或错误不得包含其值。
- `advertiser_id` 为必填广告账户 ID。
- 本任务的 `data_level` 仅使用 `CAMPAIGN`。
- `query_ids` 必填且必须全部属于同一个 `advertiser_id`。生产只读探测确认：省略该参数返回 `40002`，空数组返回 `52404`；接口不接受 `ADVERTISER` 层级。

### 状态接口

- 单次最多传 200 个 `query_ids`。
- 返回对象 ID 及其当前保护状态，供鉴权、权限和对象状态校验；当前状态不直接替代按日历史金额。

### 历史接口

- 请求包含 `advertiser_id`、`data_level`、`query_ids`、`start_date`、`end_date`。
- TikTok 接口能力窗口仍可达到最近 60 天，但本任务策略限制为最近 30 个完整自然日，并按单日调用；单批最多 200 个 Campaign ID。
- 明细字段映射：

| TikTok 字段 | 落表字段 | 处理 |
| --- | --- | --- |
| `record_date` | `record_date` | 按接口自然日保存 |
| `data_level` | `data_level` | 仅 `CAMPAIGN` |
| `query_id` | `query_id` | 原样字符串保存 |
| `bid_protection_daily_status` | `protection_status` | 原样枚举保存 |
| `status_detail` | `status_detail` | 原样文本保存，可为空 |
| `credit_amount` | `credit_amount_scaled` | 原始整数保存 |
| `credit_amount / 100000` | `credit_amount` | Decimal 精确除法，5 位小数 |
| `currency` | `currency` | 原币种保存；零赔付时允许空串 |

顶层响应按 TikTok 通用结构校验 `code`、`message`、`request_id` 和 `data`。只有 `code == 0` 且明细逐行通过校验时才进入 upsert。

## 错误码

| 类别 | 处理 |
| --- | --- |
| HTTP 429、暂时性 5xx | 指数退避并做有限次数重试；耗尽后记录账户/层级/批次失败 |
| Token 失效或无权限 | 不循环重试；任务失败且保留已有数据 |
| 参数、对象归属或日期错误 | 该批次失败，记录脱敏错误，不做猜测写入 |
| 响应缺字段、币种超长、金额不能精确解析 | 拒绝该行/批次并返回非成功状态；零赔付允许空币种 |
| 部分账户失败 | 保留其他成功 upsert；退出码仍表示本轮不完整 |

## 兼容性说明

- 状态枚举当前包括 `UNDER_PROTECTION`、`CONFIRMING`、`INELIGIBLE`、`PAYMENT_COMPLETE`、`TARGET_MET`；未知新枚举拒绝写入并报错，待契约评审后再支持，避免误判待结算状态。
- `credit_amount` 的 `100000` 缩放是接口契约，不受币种小数位影响。
- 上游按日更新，历史记录可能延迟转为终态；每天两次的任务在最近 14 天窗口内回刷明确的两个待结算状态。
- TikTok 修改字段或窗口限制时，先更新契约测试再调整同步逻辑。
