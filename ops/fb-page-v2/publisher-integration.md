# 发布系统接入香港 Page 画像 V2

发布机通过内网发送 `POST http://10.89.0.125:8944/api/integrations/v2/drama-page-assignments`，使用现有画像分配接口的 Bearer 和 `Content-Type: application/json`。可从发布机用 `GET http://10.89.0.125:8944/healthz` 检查连接。选页接口只计算并记录 Page 决定。

## 业务请求

在发布任务和素材关系已提交后调用。每个请求对应一个产品、一个实际授权 Page 池和一个稳定的业务子批。单批接受 1 至 100 个剧 ID，保留输入次序与重复项。超过 100 个时，按稳定顺序分批，在重试时沿用相同的子批号、池和剧 ID 顺序。

```json
{"external_task_id":"cross:3053734:0","page_group_id":62,"drama_ids":["DjeK3YIta1","3H5QDVSe4u"]}
```

跨模板任务使用 `cross:<ads_cross_template_queue.id>:<子批号>`。单模板原生发布使用 `native:<ads_template_make_queue.id>:<子批号>`。跨模板父任务若包含多个授权 Page 池，请分别传具体原生子任务。1841 的已核实实验模板沿用 62 组，其余按发布任务和所用账号的实际授权池取数。

成功响应含 `task_id` 和 `results` 数组。每项的 `drama_id` 与输入一一对应，有画像匹配时 `page_id` 为字符串，无匹配时为 JSON `null`。开发端逐项处理，命中项采用返回的 Page，空项沿用发布后台的兜底策略。保存返回的 `task_id` 及最终实际发布 Page，方便对账。

| HTTP | 发布方处理 |
|---|---|
| 200 | 读取全部结果。同键同内容重复调用返回最初的决定和 task_id。 |
| 400 / 413 | 核对请求字段、编码、批大小和请求体大小。 |
| 401 / 403 | 核对 Bearer、来源机器和任务对 Page 池的授权。 |
| 409 | 同一业务子批已处理过不同内容，按原始子批对账。 |
| 503、连接中断或超时 | 先保留原子批键，在调用方剩余时间允许时按原内容重试。已落盘的决定可以读回。 |

给一次 HTTP 调用预留至多 50 秒，包括连接和读取响应。服务端业务处理预算 47 秒。任务级重试应沿用同一子批键，避免多台工作机同时重试同一笔。发布方执行降级时记录原因与最终实际 Page。

## 飞书异常通知

香港能收到的请求异常由 V2 上报。若发布机连不上香港或读取响应超时，请从发布进程通过独立 HTTPS 接口 `POST https://ai.yingliangads.com/api/integrations/v2/drama-page-alerts` 上报，鉴权方式相同。郜远收到首次故障、同原因五分钟汇总与恢复通知。示例和事件幂等规则见 [告警接入说明](alerts-integration.md)。

香港审计表为 `ads_ai.fb_drama_page_assignment_log`，每笔 V2 决定用 `v2_` 开头的 task_id。条目按 `item_index>0` 查询，`item_index=0` 用于请求头或共享快照。后台确认 MySQL 条数、版本和哈希后释放本地详情和已确认快照，保留原结果及回执供重试。
