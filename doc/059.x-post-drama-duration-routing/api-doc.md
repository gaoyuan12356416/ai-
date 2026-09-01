# API 文档

## 接口列表

不新增公开 endpoint。扩展既有 schedule plan query/create、queue publish、admin logs 与 drama episodes DTO。

## 请求/响应

- 新 drama schedule candidate 在开关开启时必须带 `delivery_mode=duration_pending`、`media_validation_mode=deferred`、空 relay 与 0 media evidence。非 drama/非 schedule 请求禁止该值。
- 队列响应新增或补全：`delivery_mode`（可为逻辑 `duration_pending`）、`route_state`、`resolved_delivery_mode`、`queue_status`（可为 `waiting_relay`）、`preflight_duration/width/height`、relay ID/username、repost status。
- waiting publish 响应可只包含 `status=waiting_relay`、`queue_id`、`delivery_mode=duration_pending`、最终时长与已知错误码，不含 `log_id` 或 URL。
- admin logs 与 drama episodes 只展示运营所需的路线、最终时长和宽高；最终 URL、SHA-256、文件大小及内部 media validation 字段不下发浏览器。

## 错误码

- `x_post_premium_relay_unavailable`：长片暂无合格同语言 relay；队列可处于 waiting，未发生 X 写入。
- `x_post_drama_route_pending`：未解析路线试图绕过 resolver 创建日志，DB/Python fail closed。
- `x_post_drama_duration_routing_disabled`：兼容/回滚阶段安全停放已有 pending/waiting，零 X 写入。
- `media_preflight_changed`：最终媒体证据漂移，首次 X 写入前拒绝。
- 既有 unknown/retry/account/token/repair 错误语义不变。

## 兼容性说明

物理 queue delivery enum 不变；companion route table 通过逻辑 overlay 对外展示 pending。功能开关默认关闭。历史无 companion 的 direct/relay/141 队列完全沿用旧合同。
