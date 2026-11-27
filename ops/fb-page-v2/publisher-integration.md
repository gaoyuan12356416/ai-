# 发布系统接入香港 Page 画像 V2

发布机通过内网发送 `POST http://10.89.0.125:8944/api/integrations/v2/drama-page-assignments`，使用现有画像分配接口的 Bearer 和 `Content-Type: application/json`。可用 `GET http://10.89.0.125:8944/healthz` 检查连接。接口只筛选并记录 Page 决定。

## 业务请求

每次请求直接按传入的 Page 池和剧 ID 筛选并返回。**不按队列 ID、剧 ID 或请求内容查重，不读取旧结果，也不合并相同请求。** 相同请求再次调用也独立执行，可能返回不同的 Page。单次接受 1 至 100 个剧 ID；超过 100 个时分批调用。输入顺序和重复位置均保留，不合并同一剧的多个条目。

```json
{"external_task_id":"3092716","page_group_id":24,"drama_ids":["dqoMUlYLBQ","fhU2HzaYUX"]}
```

`external_task_id` 是调用方的追踪标识，为 1–128 字符非空字符串，可使用队列 ID。该字段不作为执行条件或去重键；相同标识传不同池、不同列表、不同分片或相同内容，均照常筛选。

Page 池需存在、未删除且属于支持的产品。按池所属产品解析剧 ID 的语言和画像，仅从具备权限且状态正常的 Page 中筛选。保留接口身份、来源机器和参数校验，以及筛选必需的 Page 池、Page 状态和剧目画像数据读取。

成功响应含 `task_id` 和 `results` 数组。每次执行生成独立 `task_id`；每项 `drama_id` 与输入一一对应，匹配时 `page_id` 为字符串，无匹配时为 JSON `null`。命中项使用返回的 Page，空项沿用发布方既有兜底策略。记录本次 `task_id` 和最终实际发布 Page 便于对账。

## 执行记录

筛选完成后追加本次执行回执，再返回结果。保存前不查询旧请求；本地回执键为独立执行标识，快照保留原 `external_task_id`、HTTP `request_id` 和 `execution_mode=direct-per-request`。请求内容摘要仅用于审计，不参与去重。

历史回执和原有 `queue_drama` 数据保留，新请求不读写原去重表。没有数据库迁移、历史回执覆盖或发布队列重置。后台审计同步的防重、未知写入回读和确认后清理继续有效，只约束同一次审计写入，不阻止新的选页请求。

香港审计表为 `ads_ai.fb_drama_page_assignment_log`，本次执行使用返回的 `task_id` 查询；`item_index>0` 为结果，`item_index=0` 为请求头或共享快照。原调用方标识在请求头 `diagnostics_json.snapshot.external_task_id` 中。确认 MySQL 条数、版本和哈希后释放本地详情和已确认快照，保留结果及回执用于审计。

| HTTP | 发布方处理 |
|---|---|
| 200 | 读取本次结果，每次调用都有独立 task_id。 |
| 400 / 413 | 核对请求字段、编码、批大小和正文大小。 |
| 401 / 403 | 核对身份、来源机器和 Page 池有效性。 |
| 503、连接中断或超时 | 记录异常；再次请求会重新筛选，不复用上一调用的结果。 |

一次 HTTP 调用预留至多 50 秒，包括连接和读取响应。服务端处理预算为 47 秒。发布方执行降级时记录原因和最终实际 Page。

## 飞书异常通知

香港收到的异常由 V2 上报；发布机连不上香港或读取超时，可通过独立接口 `POST https://ai.yingliangads.com/api/integrations/v2/drama-page-alerts` 上报，鉴权方式相同。郜远收到首次故障、同原因五分钟汇总和恢复通知。故障原因与处理说明使用中文；旧请求冲突保留供核对，新请求不会因历史队列和剧组合而返回冲突。示例和事件规则见 [告警接入说明](alerts-integration.md)。
