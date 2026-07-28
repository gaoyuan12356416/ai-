# 测试报告

## 测试结论

总体通过。合并最新线上并发基线后，本地/发布目录自动化 139/139 项通过；生产接口、数据库映射、飞书兜底、幂等和公网契约均已验收。

## 测试范围

- Token 缺失、错误、短 Token、双 Token 轮换和非 ASCII 异常输入
- 8 字段、未知字段、长度、控制字符、RFC 3339 和 32 KiB
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

## 遗留风险

- 飞书 UUID 的官方去重窗口为一小时；超过窗口且此前发送结果未知时，理论上仍可能出现同事件编号的极端重复。
- 未指定安全私聊账号，因此未向任意员工发送生产私聊 canary；真实只读映射和 email → open_id 已验证。

## 发布建议

Go。生产版本运行正常，回滚包及校验清单已验证。

## 生产验收

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
