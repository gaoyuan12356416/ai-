# 香港 Page V2 异常播报

中心运行在 CPU 服务器，接收香港 API 和发布方的事件，向后台唯一有效的郜远账号发私聊。香港接口连接被拒绝、超时或未收到完整响应时，发布方将下列事件写入自己的持久化回执后异步上报。一次重试投递原事件时保持同一个 `id`；同一故障多次影响同一子批仍只计一个受影响子批。

`POST https://ai.yingliangads.com/api/integrations/v2/drama-page-alerts` 使用与 Page 服务相同的 Bearer 鉴权。请求体给出一个 `events` 数组，每次最多 50 项。示例中的时间是 Unix 秒，省略后按接收时间处理。

```json
{"events":[{"id":"publish-6538-02-connect-1","kind":"connection","action":"fault","external_task_id":"6538-02","page_group_id":62,"detail":"connect_timeout"}]}
```

返回 HTTP 200 且 `accepted` 与提交的事件 ID 顺序一致时，该回执已在 CPU 持久化。发布方在后续实际连通并取得响应时，生成新事件 ID，并以同一 `kind` 和 `action: recovered` 上报。正常返回的空 Page 计入报表。请勿将 Token、原始错误堆栈、SQL 或请求正文放入事件。

服务器部署使用 `alerts.service` 作为 `fb-page-v2-alerts.service`，Nginx 的既有业务域名添加 `alerts.nginx.conf` 精确路径。CPU 启用前可在回环地址 `127.0.0.1:19844/healthz` 读取消息状态汇总。投递成功后，CPU 账本 `v2-incidents.sqlite3` 的 `message.receipt` 保存飞书返回的消息 ID。发送回执不确定或发送途中进程重启时，保留 `unknown` 待核实。服务端异常后重投 CPU ingress 时，使用原始事件 ID 即可取回已持久化的接受回执。
