# 素材任务最终状态飞书播报接口

## 1. 接口信息

- 请求方法：`POST`
- 请求地址：`https://ai.yingliangads.com/api/integrations/v1/material-task-status-events`
- Content-Type：`application/json; charset=utf-8`
- 鉴权：独立 Bearer Token
- 请求体上限：32 KiB
- 时间标准：RFC 3339，必须携带时区

## 2. 请求头

| Header | 必填 | 示例 | 说明 |
| --- | --- | --- | --- |
| `Authorization` | 是 | `Bearer <由我方单独提供的Token>` | Token 不放 URL 或请求体 |
| `Idempotency-Key` | 是 | `mst-20260728-000001` | 8–128 字符；同一业务事件重试必须复用同一值 |
| `Content-Type` | 是 | `application/json; charset=utf-8` | 仅接受 JSON |

`Idempotency-Key` 允许字母、数字、点、下划线、冒号和短横线，首字符必须是字母或数字。
Bearer Token 至少 32 个字符；真实 Token 由我方通过安全渠道单独提供。

## 3. 请求字段

请求体严格包含下列十个字段，不接受额外字段。2026-07-28 本次契约升级新增
`resource_name` 和 `drama_dubbing_type`；`task_type` 为原有字段并继续保留。
旧版仅包含八个字段的请求会返回 `422 invalid_payload`，甲方必须先完成字段升级。

| 字段 | 类型 | 必填 | 最大长度 | 说明 |
| --- | --- | --- | --- | --- |
| `resource_id` | string | 是 | 128 | 资源 ID，必须按字符串传输 |
| `resource_name` | string | 是 | 255 | 资源名 |
| `task_start_time` | string | 是 | 64 | RFC 3339 且带时区；秒的小数部分可省略，提供时限 1–6 位，例如 `2026-07-28T14:30:00+08:00` |
| `drama_dubbing_type` | string | 是 | 64 | 剧集配音类型 |
| `task_type` | string | 是 | 64 | 任务类型 |
| `original_material_name` | string | 是 | 255 | 素材原始名 |
| `material_name` | string | 是 | 255 | 素材名 |
| `language` | string | 是 | 32 | 语种，推荐 BCP-47，例如 `zh-CN` |
| `final_status` | string | 是 | 64 | 最终状态 |
| `optimizer_name` | string | 是 | 100 | 优化师名称；空字符串会触发兜底群提醒 |

请求示例：

```json
{
  "resource_id": "RES-20260728-000123",
  "resource_name": "暮色心约",
  "task_start_time": "2026-07-28T14:30:00+08:00",
  "drama_dubbing_type": "AI配音",
  "task_type": "素材制作",
  "original_material_name": "episode_01_source.mp4",
  "material_name": "episode_01_final_zh.mp4",
  "language": "zh-CN",
  "final_status": "已完成",
  "optimizer_name": "张三"
}
```

## 4. curl 示例

```bash
curl --request POST \
  'https://ai.yingliangads.com/api/integrations/v1/material-task-status-events' \
  --header 'Authorization: Bearer <TOKEN>' \
  --header 'Idempotency-Key: mst-20260728-000001' \
  --header 'Content-Type: application/json; charset=utf-8' \
  --data '{
    "resource_id": "RES-20260728-000123",
    "resource_name": "暮色心约",
    "task_start_time": "2026-07-28T14:30:00+08:00",
    "drama_dubbing_type": "AI配音",
    "task_type": "素材制作",
    "original_material_name": "episode_01_source.mp4",
    "material_name": "episode_01_final_zh.mp4",
    "language": "zh-CN",
    "final_status": "已完成",
    "optimizer_name": "张三"
  }'
```

## 5. 成功响应

新事件可靠落库后返回：

```http
HTTP/1.1 202 Accepted
```

```json
{
  "code": "accepted",
  "message": "事件已接收，正在投递",
  "event_id": "MSE-0000000123",
  "duplicate": false,
  "delivery_status": "queued",
  "received_at": "2026-07-28T06:30:01.000000Z"
}
```

`202` 表示事件已可靠接收并进入投递队列，不等同于飞书已经送达。

相同幂等键和相同请求体再次提交时，服务复用原事件且不会创建新的正常播报：

```json
{
  "code": "duplicate_accepted",
  "message": "事件已接收",
  "event_id": "MSE-0000000123",
  "duplicate": true,
  "delivery_status": "delivered",
  "received_at": "2026-07-28T06:30:01.000000Z"
}
```

## 6. 错误响应

| HTTP | code | 含义 | 甲方处理 |
| ---: | --- | --- | --- |
| 400 | `idempotency_key_required` | 缺少 `Idempotency-Key` | 补充请求头 |
| 400 | `invalid_idempotency_key` | 幂等键格式不正确 | 按请求头规则修正 |
| 400 | `invalid_request` | 请求体传输方式或 `Content-Length` 不合法 | 修正 HTTP 请求 |
| 400 | `invalid_json` | JSON 无法解析 | 修正请求 |
| 401 | `invalid_token` | Token 缺失或错误 | 核对 Token |
| 409 | `idempotency_conflict` | 同一幂等键对应不同请求体 | 使用原内容重试，或为真实新事件使用新 key |
| 413 | `payload_too_large` | 请求体超过 32 KiB | 缩小请求 |
| 415 | `unsupported_media_type` | Content-Type 不是 JSON | 改为 JSON |
| 422 | `invalid_payload` | 字段缺失、额外字段、超长、非字符串或时间无时区 | 修正请求 |
| 503 | `service_unavailable` | 服务配置或事件存储暂不可用 | 保持原幂等键重试 |

错误示例：

```json
{
  "code": "invalid_payload",
  "message": "task_start_time 必须为带时区的 RFC3339 时间"
}
```

甲方只应对网络超时和 `5xx` 使用原 `Idempotency-Key` 重试；`4xx` 需要先修正请求。

## 7. 播报规则

优化师匹配路径：

```text
optimizer_name
  -> admin_users.username
  -> admin_users.id
  -> admin_user_group.sub_user_id
  -> admin_user_group.email
  -> Feishu open_id
  -> 私聊
```

名称去除首尾空白后做大小写敏感的精确匹配，不做模糊匹配或别名猜测。

无法完成私聊时，服务会把同一事件发到兜底群并携带失败码。甲方请求已经被正常接收时，不需要因为“进入兜底群”再次推送。

私聊格式：

```text
【素材任务最终状态播报】

资源ID：RES-20260728-000123
资源名：暮色心约
任务开始时间：2026-07-28T14:30:00+08:00（Asia/Shanghai，UTC+08:00）
剧集配音类型：AI配音
任务类型：素材制作
素材原始名：episode_01_source.mp4
素材名：episode_01_final_zh.mp4
语种：zh-CN
最终状态：已完成
优化师名称：张三

事件编号：MSE-0000000123
```

兜底群格式：

```text
【⚠️ 素材任务播报未能私聊】

失败原因：optimizer_not_found
说明：admin_users.username 未找到完全匹配的用户

资源ID：RES-20260728-000123
资源名：暮色心约
任务开始时间：2026-07-28T14:30:00+08:00（Asia/Shanghai，UTC+08:00）
剧集配音类型：AI配音
任务类型：素材制作
素材原始名：episode_01_source.mp4
素材名：episode_01_final_zh.mp4
语种：zh-CN
最终状态：已完成
优化师名称：张三

事件编号：MSE-0000000123
请检查优化师名称、admin_users.username、admin_user_group.email 及飞书用户映射。
```

## 8. 安全注意事项

- Token 由我方通过安全渠道单独提供，禁止放入群消息、工单截图、前端代码或公开仓库。
- 全程使用 HTTPS。
- Token 轮换期间我方可短期同时接受新旧 Token；切换完成后立即撤销旧 Token。
- 接口不使用来源 IP 白名单，来源 IP 仅用于审计。
