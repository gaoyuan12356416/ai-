# 测试报告

## 测试结论

总体通过。本次十字段增量在最新线上基线之上完成，本地自动化 139/139
项通过；生产切换前 outbox 未完成事件为 0。十字段生产 canary 已验证旧
八字段拒绝、新请求送达、幂等重放、兜底群目标和完整消息正文。

## 测试范围

- Token 缺失、错误、短 Token、双 Token 轮换和非 ASCII 异常输入
- 10 字段、旧八字段拒绝、未知字段、长度、控制字符、RFC 3339 和 32 KiB
- `resource_name`、`drama_dubbing_type` 参与规范化、幂等摘要和两种播报
- 串行/并发幂等、冲突、租约、重试、dead letter
- username 精确查询、email 映射、飞书 open_id
- 私聊、兜底、稳定消息 UUID、鉴权 Token 刷新
- worker readiness、飞书异常响应和敏感数据脱敏
- FB playable、X 账号/路由和 X 素材校验相关回归
- Python 3.9 grammar、HTML 解析和 Git diff 格式

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 素材状态纯模块 | 13 | 13 | 0 | 0 |
| Webhook HTTP 与投递编排 | 15 | 15 | 0 | 0 |
| X 账号业务回归 | 53 | 53 | 0 | 0 |
| X 应用路由契约 | 20 | 20 | 0 | 0 |
| X 多时段存储回归 | 23 | 23 | 0 | 0 |
| X 多时段界面契约 | 9 | 9 | 0 | 0 |
| X 素材校验回归 | 4 | 4 | 0 | 0 |
| FB playable 生成/文档聚合检查 | 2 | 2 | 0 | 0 |
| **合计** | **139** | **139** | **0** | **0** |

## 缺陷情况

代码评审发现的 7 项问题均已修复并回归，无未关闭 Blocker / High。

## 验证证据

- `python -m py_compile app.py features/material_status_broadcast/service.py`
- `python scripts/test_material_status_broadcast.py`：13/13
- `python scripts/test_material_status_webhook_app.py`：15/15
- `python scripts/test_x_accounts.py`（合并线上基线后）：53/53
- `python scripts/test_x_accounts_app_contract.py`（合并线上基线后）：20/20
- `python scripts/test_x_post_multi_schedule_store.py`：23/23
- `python scripts/test_x_post_multi_schedule_ui.py`：9/9
- `python scripts/test_x_post_drama_validation_app.py`：4/4
- `python scripts/test_fb_playable_generator.py`：聚合检查通过
- `python scripts/test_playable_preview_docs.py`：聚合检查通过
- Python 3.9 AST grammar：4 个变更 Python 文件通过
- `git diff --check`：通过
- 旧八字段请求：`422 invalid_payload`
- 私聊/兜底消息：均按固定顺序展示完整十字段

## 遗留风险

- 飞书 UUID 的官方去重窗口为一小时；超过窗口且此前发送结果未知时，理论上仍可能出现同事件编号的极端重复。
- 未指定安全私聊账号，因此未向任意员工发送生产私聊 canary；真实只读映射和 email → open_id 已验证。

## 发布建议

Go。生产版本运行正常，回滚包及校验清单已验证。

## 首次发布生产验收（十字段增量部署前基线）

| 检查 | 结果 |
| --- | --- |
| MySQL `@@read_only` | `1` |
| 精确 username → email | 通过 |
| email → Feishu open_id | 通过 |
| 公网错误 Token | `401 invalid_token` |
| 公网 40 KB 请求 | `413 payload_too_large` JSON |
| 来源 IP 白名单 | 无；来源 IP 仅审计 |
| 兜底群 canary | `MSE-0000000001`，一次送达 |
| 同幂等键重放 | 返回同一事件；outbox 记录数仍为 1 |
| 飞书消息回读 | 已确认指定群和资源 ID |
| Token 泄漏检查 | journal、Nginx access log、SQLite、公开文档均未发现 |
| 服务状态 | API 与 Nginx 均 active；发布后错误日志 0 |
| 公开接口文档 | HTTP 200 |
| 回滚包 | SHA-256 与 SQLite quick_check 均通过 |

## 十字段增量生产验收

| 检查 | 结果 |
| --- | --- |
| 精确提交 | `8af21dbead5fd6fcf5f048319d76971573def77c` |
| 发布目录测试 | 需求专项 28/28、相关回归及 FB playable 检查共 138 项通过 |
| 生产 Python 版本边界 | 无关的 playable 文档生成测试要求 Python 3.10+，生产默认 3.9；同一提交在本地已通过该项 |
| 切换前 outbox | `delivered=2`，`queued/retry/processing=0` |
| 旧八字段 | `422 invalid_payload`；没有创建 outbox 记录 |
| 新十字段 | `202 accepted`，事件 `MSE-0000000003` |
| 实时投递 | `delivered/fallback/attempt1`，失败码 `optimizer_not_found` |
| 幂等重放 | `202 duplicate_accepted`，同一事件且该 key 仅 1 条 outbox |
| 飞书确认 | 同一稳定 UUID 返回相同 message_id、指定兜底群和完全一致正文；十字段顺序正确且各出现一次 |
| 公开文档 | HTTP 200；公网与源文件 SHA-256 均为 `75a28bf49f96a33870b2eee45ca0ba5b5f569b3a5f095c94d7a7b3e90dbd4ca3` |
| 服务状态 | 17:32:04 起 active；发布后 warning/error journal 为 0 |
| Token | 未轮换；指纹前缀保持 `ecd760a9d90b8399` |
| 泄漏检查 | journal、Nginx 日志、SQLite、公开文档和源码均未发现 Token |
| 回滚包 | `SHA256SUMS` 全通过，SQLite `PRAGMA quick_check=ok` |

飞书应用缺少“获取会话历史消息”的只读权限，直接列表回查返回 `230027`。
验收改用系统实际发送时的同一稳定 UUID 重放：飞书返回相同 message_id，
相同 chat_id 和完全一致的响应正文，因此没有产生第二条消息。
