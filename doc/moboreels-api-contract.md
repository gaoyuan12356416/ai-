# Moboreels KOC 数据接口契约

来源：[已发布] KOC 接口文档 v6.3，https://cvbadml3pe5.feishu.cn/docx/AAXydWTWKoQ0Ahx264pcXyWEndD 。以下按 2026-11-12 已读取的文档正文整理，不代表供应方后续版本。未记录任何机构值、凭据或真实业务行。

## 通用

- POST JSON；域名 `https://kocapi-cn.cdreader.com`；需 IP 白名单。
- Header：`reqid` 唯一且只能请求一次；`timestamp` 为 13 位毫秒时间戳，允许前后 5 分钟；`instuserid` 为机构 ID；`sign = SHA256(lowercase(reqid_timestamp_instuserid_token))`。先将下划线拼接的原文整体小写，再计算摘要。Token 不直接作为 Header 传输。
- `project_type`：1 网文，2 短剧。
- 文档没有定义统计时区、每日更新的时区、时间范围端点是否包含；不能把本地调度时区当作供应方统计时区。

## POST 获取广告收益

完整 URL：`https://kocapi-cn.cdreader.com/api/story/getadlist`

| 参数 | 类型 | 必填 | 文档说明 |
|---|---|---|---|
| date | string | 是 | 日期，示例 `2025-08-02` |
| project_type | int | 是 | 业务类型，见枚举 |
| page_index | int | 是 | 当前页码，默认 1 |
| page_size | int | 是 | 默认 10，最大值 100 |

文档规定：每日 + 口令返回一条聚合。接口概览写明 T+1 提供到昨日，次日早上 9 点更新；未定义时区、回补/修订窗口。

成功响应结构：

```json
{
  "code": 200,
  "message": "Success",
  "data": {
    "total": 1,
    "list": [
      {
        "ad_time": "2025-08-02",
        "project_type": 2,
        "code": "25FA",
        "amount": 100.0000
      }
    ]
  }
}
```

响应行字段：`code` 口令词；`amount` 金额，单位美分；`project_type` 业务类型；`ad_time` 广告收入时间。原文没有广告行 ID、达人 ID、剧目 ID、推广链接 ID。金额可能带小数，不能先取整美分。换算美元应精确除以 100。

## POST 获取订单明细

完整 URL：`https://kocapi-cn.cdreader.com/api/story/getorder`

`begin_time` 与 `end_time` 必须同时填写，最大支持查询 31 天。

| 参数 | 类型 | 必填 | 文档说明 |
|---|---|---|---|
| begin_time | timestamp 数值 | 是 | 开始时间，13 位毫秒时间戳 |
| end_time | timestamp 数值 | 是 | 结束时间，13 位毫秒时间戳 |
| code | string | 否 | 口令词 |
| story_id | string | 否 | 故事 ID |
| page_index | int | 是 | 当前页码，默认 1 |
| page_size | int | 是 | 默认 10，最大值 100 |
| project_type | int | 是 | 业务类型，见枚举 |
| status | int | 否 | -1 全部；0 充值订单（默认）；1 已退款 |

成功外层为 `code: 200`、`message`、`data: {total, list}`。

| 响应行字段 | 文档说明/示例类型 |
|---|---|
| create_time | 订单创建时间；示例 `2024-09-21 17:49:50`，没有时区 |
| code | 口令词 |
| status_text | 充值订单、退款订单 |
| ref_order_id | 外部订单号 |
| story_id | 故事 ID（小说/短剧 ID），示例为数字 |
| story_name | 故事名称 |
| amount | 美元；充值/退款原始金额，示例 `3.0000` |
| project_type | 业务类型；说明引用枚举，但示例为中文字符串 `网文` |
| coo_order_status | 订单状态，1 成功 |
| institution_user_id | 机构用户 ID |
| star_user_id | 供应方达人用户 ID，即达人登录 KOC 平台的账号 |
| real_star_user_id | 调用推广/口令接口时我方传入的达人用户 ID |

原文没有单独的订单数值 `status` 响应字段；请求 `status` 与响应 `coo_order_status` 不能互换。退款原始金额不能直接当作带负号的净收入。订单和广告收入金额单位不同。

## POST 创建推广信息

完整 URL：`https://kocapi-cn.cdreader.com/api/story/getcodeinfo`

必填 JSON：`story_id`（string）、`star_user_id`（我方达人唯一 ID，string，最多 32）、`star_name`（string）、`project_type`（int）。该接口创建推广信息。

成功 `data` 示例只列出：`story_id`、`project_type`、`koc_code`、`deep_link`、`home_link`。本聊天已有成功回执的字段也与此一致，均未见 `id`、`link_id`、`promotion_id` 等供应方推广链接 ID。

可证实的关联是：统计响应 `code` 为口令词，创建响应 `koc_code` 为推广口令。我方可保存自己的推广记录 ID，并用机构、业务类型和口令将收入关联到该记录；不能声称我方记录 ID 是供应方返回的链接 ID。若同一口令对应多条我方记录，不能不经核对任意选择一条分配收入。

归因描述：输入口令后用户所有充值归 KOC，后输入其他 KOC 口令覆盖此前归属。文档未说明是否重算历史订单、有效期、跨设备行为。

## 尚未明确

- 统计时区、T+1 9 点的时区、订单时间边界、广告收益的修订窗口。
- 错误码、QPS、创建接口的业务幂等、超时后的创建结果查询方式。
- 未见独立供应方推广链接 ID；不可猜测新字段或拼造关联 ID。
- `getcodeinfo` 的 data 字段解释有一部分嵌入内容不能在提取文本中展示，以上字段来自其可见 JSON 示例与已有真实回执。
