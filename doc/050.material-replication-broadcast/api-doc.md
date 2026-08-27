# 自动复刻批次飞书播报接口

接口版本：v1 · 文档日期：2026-08-27 · **状态：待上线**

本文是待上线合同，不代表地址现已可用或已完成真实飞书送达验收。上游发送已经汇总的自动复刻事件，接收方可靠接收后尽快异步通知对应剪辑师，不创建或重试复刻任务。

配套文件：[独立 HTML](api-doc.html) · [OpenAPI 3.0.3](openapi.json) · [发起示例](examples/started.json) · [失败示例](examples/failed.json)。本文包含完整字段、请求、响应和投递规则，无需查阅旧接口文档即可对接。

## 1. 接口与职责

```text
POST https://ai.yingliangads.com/api/integrations/v1/material-replication-events
```

| 事项 | 上游负责 | 接收方负责 |
| --- | --- | --- |
| 汇总周期 | 已发起每 2 小时、失败每 1 小时汇总；跨批次只推新增 | 接收后尽快异步投递，不再增加汇总周期 |
| 批次划分 | 每次一个剪辑师、一个事件类型，最多 50 条素材 | 整批校验、持久化接收，保持条目和失败语种顺序 |
| 去重 | 持久化业务批次/幂等键/原内容；避免跨批次业务重复 | 仅对同一幂等键防重复；相同内容换键仍视为新批次 |
| 超限 | 适当缩小并拆成新子批次，为各子批次分配新键 | 超限拒绝，不截断、不自动拆批 |
| 通知对象 | 仅填写剪辑师系统用户名 | 精确映射私聊，符合规则才用既有兜底群 |

旧 `POST /api/integrations/v1/material-task-status-events` 保持不变，使用自己的 Token、最终状态合同和重试队列。同一次复刻失败不要再同时调用旧接口，否则会产生业务重复播报。

新接口不接收 `task_id`、产出素材、`final_status`、手机号、邮箱、`open_id` 或群 ID；不读取/写入素材复刻业务表，不新增资源 20 分钟提醒，不触发真实复刻或复刻重试。

## 2. 请求头与鉴权

| 请求头 | 必填 | 规则 |
| --- | --- | --- |
| `Authorization` | 是 | `Bearer <本接口专属Token>`；Token 至少 32 个无空白 ASCII 字符，与旧接口分开配置，安全渠道单独提供 |
| `Content-Type` | 是 | `application/json`；请求体编码 UTF-8，可附 `charset=utf-8` |
| `Idempotency-Key` | 是 | 8–128 个 ASCII 字符，匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$` |

幂等键示例：`mrb-start-editor-demo-20260827-1400-001`。键在首次发送前应由上游落库；不要每次 HTTP 重试临时生成一个新键。

HTTP 客户端须提供唯一、合法的 `Content-Length`（curl 会自动生成）。不支持分块传输或压缩请求体；JSON 对象内字段名不得重复，不能使用 `NaN` / `Infinity` 等非 JSON 数值。

全程使用 HTTPS。不要把 Token 放在 URL、请求体、前端页面、截图、日志、群消息或公开代码中。以下 curl 仅引用安全注入的环境变量，不包含真实 Token；上线并完成授权前不要向生产地址执行示例。

## 3. 请求体与校验

顶层 JSON 对象恰好包含三个必填字段，不允许其他字段。

| 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- |
| `event_type` | string | 是 | 只能为 `replication_started`（已发起）或 `replication_failed`（失败） |
| `editor_username` | string | 是 | 剪辑师的系统用户名，单行可显示文本；去除首尾空白、NFC 规范化后 0–100 个 Unicode 字符；空字符串允许，直接进入兜底群 |
| `items` | array | 是 | 1–50 条素材；同一剪辑师、同一事件类型，顺序保留 |

每条 `items` 对象的字段如下，除 `failed_languages` 的事件条件外均必须存在。

| 字段 | 类型 | 长度 / 数量 | 规则 |
| --- | --- | --- | --- |
| `resource_id` | string | 1–128 | 资源 ID，非空单行；数字 ID 也必须加引号 |
| `resource_name` | string | 1–255 | 资源名，非空单行 |
| `original_material_id` | string | 1–128 | 原始素材 ID，非空单行 |
| `original_material_name` | string | 1–255 | 原始素材名，非空单行 |
| `failed_languages` | string[] | 1–32 项，每项 1–100 字符 | **失败事件每项必填；已发起事件必须省略**，不是传 `[]`；元素非空单行，顺序保留 |

文本去除首尾空白后做 Unicode NFC 规范化，大小写保持不变。字段长度按规范化后的 Unicode 字符数计算，不按 UTF-8 字节数计算；纯空白的必需文本仍视为空值。不接受多行素材信息、`null`、数字代替字符串或类型自动转换。

所有字符串拒绝控制字符、零宽格式字符、孤立代理码点及 Unicode 行/段分隔符（类别 `Cc` / `Cf` / `Cs` / `Zl` / `Zp`）；即使这些字符位于首尾，也不能当作可修剪空白。普通首尾空格可去除。

任何缺字段、额外字段、非法事件类型、空数组、超长或不合法条目都会整批拒绝，不部分接收。请求仅允许一个用户名；请用系统中的 `admin_users.username`，不要猜测显示名或别名。

### 两层大小限制

| 限制对象 | 上限 | 超限响应 |
| --- | --- | --- |
| HTTP 原始请求体的 UTF-8 字节数 | 32 KiB = 32,768 字节 | `413 payload_too_large` |
| 最终飞书请求 JSON 的 UTF-8 字节数 | 128 KiB = 131,072 字节 | `413 message_too_large` |

第二层包含所有条目、固定文案、页脚、兜底包装和 JSON 转义开销，不能仅用 `items` 数量或字符数判断。即使原请求小于 32 KiB，最终消息仍可能超限。接收方不自动拆批或截断；整批拒绝后，上游拆为更小的新批次并为各新子批次分配新键。已经返回 `202` 的批次不能以“拆批”为由换键重发相同内容。

最终消息大小预检针对新批次；已接收批次的同键同内容重提读取原批次状态，不因未来模板文案扩大而重新拒绝，也不重新渲染或发送。

128 KiB 是本接口的保守边界，不是飞书官方限制的原文。2026-08-27 核对的飞书官方 SDK v3.10.0 文档将文本消息请求体上限记为 150 KB，并要求内容 JSON 转义；调用本接口仍以 128 KiB 为准。[官方 SDK：CreateMessageReqBody](https://pkg.go.dev/github.com/larksuite/oapi-sdk-go/v3/service/im/v1#CreateMessageReqBody)

## 4. 完整请求示例

示例中的资源、素材和 `editor_demo` 均为演示值；正式调用必须换成真实业务值。

### 4.1 已发起批次

```json
{
  "event_type": "replication_started",
  "editor_username": "editor_demo",
  "items": [
    {
      "resource_id": "RES-20260827-001",
      "resource_name": "暮色心约",
      "original_material_id": "MAT-000101",
      "original_material_name": "暮色心约_原始素材_01.mp4"
    },
    {
      "resource_id": "RES-20260827-002",
      "resource_name": "星河与你",
      "original_material_id": "MAT-000102",
      "original_material_name": "星河与你_原始素材_02.mp4"
    }
  ]
}
```

### 4.2 失败批次

```json
{
  "event_type": "replication_failed",
  "editor_username": "editor_demo",
  "items": [
    {
      "resource_id": "RES-20260827-001",
      "resource_name": "暮色心约",
      "original_material_id": "MAT-000101",
      "original_material_name": "暮色心约_原始素材_01.mp4",
      "failed_languages": ["法语", "日语"]
    },
    {
      "resource_id": "RES-20260827-002",
      "resource_name": "星河与你",
      "original_material_id": "MAT-000102",
      "original_material_name": "星河与你_原始素材_02.mp4",
      "failed_languages": ["西班牙语", "韩语"]
    }
  ]
}
```

### 4.3 curl

以下为 Bash 示例。在含 `examples` 的文档目录执行，`MATERIAL_REPLICATION_TOKEN` 应由安全环境注入。两个业务批次使用不同的幂等键，重试时复用各自原键与原文件内容。

```bash
curl --request POST \
  'https://ai.yingliangads.com/api/integrations/v1/material-replication-events' \
  --header "Authorization: Bearer ${MATERIAL_REPLICATION_TOKEN}" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: mrb-start-editor-demo-20260827-1400-001' \
  --data-binary '@examples/started.json'

curl --request POST \
  'https://ai.yingliangads.com/api/integrations/v1/material-replication-events' \
  --header "Authorization: Bearer ${MATERIAL_REPLICATION_TOKEN}" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: mrb-failed-editor-demo-20260827-1400-001' \
  --data-binary '@examples/failed.json'
```

## 5. HTTP 202 响应

首次可靠落库接收：

```json
{
  "code": "accepted",
  "message": "批次已接收，等待投递",
  "batch_id": "MRB-0000000001",
  "duplicate": false,
  "item_count": 2,
  "delivery_status": "queued",
  "delivery_kind": "",
  "received_at": "2026-08-27T06:00:00Z"
}
```

原键原内容重复提交的示例（这次读取时已确认私聊送达）：

```json
{
  "code": "duplicate_accepted",
  "message": "批次已接收",
  "batch_id": "MRB-0000000001",
  "duplicate": true,
  "item_count": 2,
  "delivery_status": "delivered",
  "delivery_kind": "private",
  "received_at": "2026-08-27T06:00:00Z"
}
```

| 响应字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 首次为 `accepted`，同键同内容为 `duplicate_accepted` |
| `message` | string | 可读提示；业务判断使用 `code` 和状态字段，不依赖提示文案 |
| `batch_id` | string | 服务生成的批次号，例如 `MRB-0000000001`；重复请求保持原号 |
| `duplicate` | boolean | 是否命中已存在的同键同内容批次 |
| `item_count` | integer | 本批素材数，1–50；不是语言数或复刻成功数 |
| `delivery_status` | string | 当前异步投递状态，见下表 |
| `delivery_kind` | string | 仅确认送达后为 `private` 或 `fallback`；其他状态为空字符串 `""`，不是 `null` |
| `received_at` | string | 首次接收时间，RFC3339 UTC，以 `Z` 结尾；重复请求不改变 |

| `delivery_status` | 含义 | `delivery_kind` |
| --- | --- | --- |
| `queued` | 已持久化，待处理 | `""` |
| `processing` | 正在处理 | `""` |
| `retry` | 等待本功能的有限重试 | `""` |
| `delivered` | 已确认投递成功 | `private`（私聊）或 `fallback`（兜底群） |
| `dead_letter` | 明确不可恢复，已停止自动处理 | `""` |
| `delivery_unknown` | 无法确认是否送达，已停止盲目重试/兜底 | `""` |

**202 不等于飞书已经送达；兜底群送达不等于剪辑师私聊送达。** 重复请求返回原批次的当前状态，不会自动重置 `delivered`、`dead_letter` 或 `delivery_unknown`。本期没有必需的 GET 查询接口；对账可原键原内容重新 POST。

## 6. 幂等与上游重试

同一键只对应一个业务批次。服务比较规范化后的全部字段，保留 `items` / `failed_languages` 顺序；JSON 对象字段顺序无关。相同键的内容或数组顺序改变即 `409 idempotency_conflict`。

| 结果 | 上游处理 |
| --- | --- |
| 网络超时、连接中断、`5xx` | 保留原键和原内容，按 1、5、30、120 秒间隔重试；不要创建替代批次 |
| `202 accepted` / `202 duplicate_accepted` | 停止接收重试；如需对账仍用原键原内容读取，不为了“再发一次”换键 |
| 其他 `4xx` | 先按错误修正请求；不盲目重试 |
| `409 idempotency_conflict` | 核对已存原批次与本次内容；不换键绕过冲突或掩盖未知结果 |
| `413` | 被拒绝的批次由上游缩小后拆成真正的新子批次，各用新键 |

接收端只防同键重复，不保证相同业务内容跨新键去重。上游应避免跨周期重复，也不要让同一复刻失败同时走新旧接口。外部通信可能出现未知结果，因此本接口不承诺端到端严格 exactly-once。

## 7. 错误响应

错误体固定为可读的 `code` / `message`，不返回凭据、内部地址或异常栈。

```json
{
  "code": "invalid_payload",
  "message": "items[0].failed_languages 必须为非空数组"
}
```

| HTTP | `code` | 含义 | 处理 |
| ---: | --- | --- | --- |
| 400 | `invalid_request` | Content-Length 不合法、分块/压缩传输，或请求体接收超时/中断 | 修正 HTTP 请求 |
| 400 | `invalid_json` | 无法解析有效 UTF-8 JSON、重复对象字段或非 JSON 数值 | 修正 JSON 编码与语法 |
| 400 | `idempotency_key_required` | 缺少幂等键 | 增加请求头 |
| 400 | `invalid_idempotency_key` | 幂等键长度或字符不符合规则 | 修正为 8–128 字符的规定格式 |
| 401 | `invalid_token` | Token 缺失或不正确 | 核对本接口专属 Token，不使用旧 Token |
| 409 | `idempotency_conflict` | 原键已对应不同内容/顺序 | 核对原批次，不能盲目换键重发 |
| 413 | `payload_too_large` | 原始请求体超过 32 KiB | 上游缩小批次 |
| 413 | `message_too_large` | 最终飞书请求 JSON 超过 128 KiB | 上游缩小批次，考虑兜底包装与转义开销 |
| 415 | `unsupported_media_type` | Content-Type 不是 JSON | 使用 `application/json` |
| 422 | `invalid_payload` | 字段/类型/数量/长度或事件专属字段不合法 | 整批修正；无部分接收 |
| 503 | `service_unavailable` | 配置或可靠存储暂不可用 | 原键原内容退避重试，持续失败时联系维护方 |

## 8. 收件人、兜底与未知结果

映射链仅由服务控制：

```text
editor_username
  → admin_users.username（大小写敏感、精确匹配）
  → admin_users.id
  → admin_user_group.sub_user_id（有效 status=0）
  → admin_user_group.email（复用既有用户名/邮箱映射缓存）
  → 按邮箱查询飞书得到 open_id
  → 首次发送前冻结目标，通知对应剪辑师私聊
```

空 `editor_username` 允许接收，直接进入既有兜底群。未找到唯一有效映射、缺少有效邮箱/open_id，或收到确定的私聊失败结果时，使用既有兜底群。消息保留原批次正文并补充中文原因，不向调用方公开内部群标识。

复用的是用户名/邮箱身份映射缓存，不是 open_id 缓存。每个新批次解析出飞书 open_id 后，在首次发送前冻结目标；已冻结请求的重试不重复选择或更换收件人。

发送前将目标、正文和 UUID 持久化冻结。网络超时或连接中断不等于发送失败：如果不能确认飞书是否已经接收，只在最多 3300 秒的安全重试窗口内，对同一冻结目标、正文和 UUID 进行有限重试，最多 5 次投递尝试。仍不能确认或超出窗口则标记 `delivery_unknown`，不改目标、不换 UUID、不盲目转发兜底群。

3300 秒是我方留有余量的重试边界，不是官方 UUID 时限。官方 SDK 对相同 UUID 描述为 1 小时内最多成功发送一条消息；本接口不能据此承诺跨任意时间的 exactly-once。[官方 SDK：Uuid 字段](https://pkg.go.dev/github.com/larksuite/oapi-sdk-go/v3/service/im/v1#CreateMessageReqBody)

已接收的批次若进入兜底、死信或未知状态，不需要上游再创建新键发送一遍；保留批次号供维护方对账。新功能的重试策略与旧接口完全隔离。

## 9. 飞书消息示例

### 9.1 已发起

```text
【自动复刻任务已发起】

以下素材已自动发起复刻任务：

1. 资源ID：RES-20260827-001
   资源名：暮色心约
   原始素材ID：MAT-000101
   原始素材名：暮色心约_原始素材_01.mp4

2. 资源ID：RES-20260827-002
   资源名：星河与你
   原始素材ID：MAT-000102
   原始素材名：星河与你_原始素材_02.mp4

素材语种包含：西班牙语、法语、阿拉伯语、俄语、葡萄牙语、日语、繁体中文、泰语、印度尼西亚语、德语、越南语、意大利语、土耳其语、波兰语、罗马尼亚语、捷克语、韩语。

批次编号：MRB-0000000001
```

“素材语种包含”是固定业务说明，不代表 17 个语种已全部复刻成功，也不是调用方可配置的执行参数。

### 9.2 失败

```text
【自动复刻失败】

以下素材自动复刻失败：

1. 资源ID：RES-20260827-001
   资源名：暮色心约
   原始素材ID：MAT-000101
   原始素材名：暮色心约_原始素材_01.mp4
   失败语种：法语、日语

2. 资源ID：RES-20260827-002
   资源名：星河与你
   原始素材ID：MAT-000102
   原始素材名：星河与你_原始素材_02.mp4
   失败语种：西班牙语、韩语

备注：复刻失败一般是算法失败，重试基本也不会成功。

批次编号：MRB-0000000002
```

失败语种来自每项 `failed_languages`，用“、”按原顺序连接。固定备注是一般性业务提示，不是本接口对每次失败根因的算法诊断；通知失败也不会生成复刻重试任务。

## 10. 上线前对接清单

- 确认本接口已上线并从安全渠道取得专属 Token；当前文档状态仍为待上线。
- 使用真实系统用户名，不传显示名、邮箱、手机号或 open_id；用户名为空时应明确接受兜底群通知。
- 按剪辑师/事件类型划分批次，失败逐项带失败语种，发起逐项不带该字段。
- 上游持久化键与原内容，按既定周期仅推新增，避免与旧最终状态接口双发。
- 接收重试与业务补发分开：超时/5xx 原键原内容，202 后不换键重复发送。
- 验证双大小上限、所有字段边界和错误分支；只做批准的联调，不自动向生产发送演示消息。
- 以 `delivery_status` 与 `delivery_kind` 解释投递结果，保留 `batch_id` 对账，不把 202 或兜底送达当成私聊送达。
