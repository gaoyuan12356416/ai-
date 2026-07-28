# SA 需求与设计评审

## 结论

有条件通过。必须落实独立 Token、持久化幂等、严格名称匹配、只读数据库、飞书失败兜底和敏感信息保护。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 决策 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | 高 | 鉴权 | 仅依靠固定 URL 无法防止未授权调用 | 使用独立高熵 Bearer Token，支持双 Token 轮换，恒定时间比较 | 已接受 |
| SA-002 | 高 | 幂等 | 甲方超时重试可能产生重复私聊和重复兜底告警 | 强制 `Idempotency-Key`，事件表唯一约束并校验 payload hash | 已接受 |
| SA-003 | 高 | 匹配 | 现有按 `admin_user_group.name` 查询不符合需求 | 严格走 `admin_users.username -> id -> admin_user_group.sub_user_id` | 已接受 |
| SA-004 | 高 | 可靠性 | 同步发送后才响应会把飞书故障传递给甲方 | 先落库返回 `202`，后台 outbox 实时投递并重试 | 已接受 |
| SA-005 | 高 | 安全 | Token、email、open_id 可能进入日志或响应 | Token 永不记录；email 仅脱敏；open_id 不落库 | 已接受 |
| SA-006 | 中 | 时间 | 无时区时间会产生歧义 | 强制 RFC 3339 带时区，UTC 保存、UTC+8 展示 | 已接受 |
| SA-007 | 中 | 兜底 | 只覆盖“用户名未命中”仍可能丢失消息 | email/飞书未命中、私聊最终失败也进入同一兜底群 | 已接受 |
| SA-008 | 中 | 范围 | 初始方案包含来源 IP 白名单 | 用户明确改为只用 Token；IP 仅作审计，不参与鉴权 | 已接受 |
| SA-009 | 高 | 接口契约 | 新增两个必填字段会使旧八字段请求不再通过严格校验 | 保持同一路径 `v1`，明确切换时点；公开文档标注旧八字段返回 `422`，甲方上线前必须同步升级 | 已接受 |

## 决策记录

- API 版本固定为 `v1`。
- 对外只接受 `Authorization: Bearer`，不兼容 `X-API-Token`。
- 不用 `resource_id` 单独去重。
- 甲方不可传入 `dry_run` 或目标飞书 ID。
- 业务匹配失败立即兜底；临时依赖错误有限重试后兜底或 dead letter。
- 当前严格契约为十字段；新增 `resource_name`、`drama_dubbing_type`，
  保留既有 `task_type`，三项均进入私聊和兜底播报。

## PM 修订确认

上述意见及十字段契约升级已反映到 `requirements.md`、`api-doc.md`、
`test-cases.md` 和 `deploy.md`。
