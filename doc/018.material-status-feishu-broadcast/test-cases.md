# 测试用例

## 测试范围

Token 鉴权、字段校验、幂等、持久化队列、数据库映射、飞书用户解析、私聊、兜底群、重试、重启恢复和敏感信息保护。

## 测试数据

- 所有资源 ID 使用 `TEST-` 前缀。
- 自动化测试使用临时 SQLite、假 MySQL 结果和假 Feishu 适配器。
- 生产兜底 canary 使用不存在的测试优化师并在消息中明确“联调测试”。
- 未经确认不执行真实私聊 canary。

## 用例列表

| 编号 | 场景 | 预期结果 | 优先级 |
| --- | --- | --- | --- |
| TC-001 | 正确 Bearer、幂等键和合法十字段 | `202 accepted`，事件落库 | P0 |
| TC-002 | Token 缺失或错误 | `401 invalid_token`，不落库、不发飞书 | P0 |
| TC-003 | 使用 `X-API-Token` 但无 Bearer | `401` | P0 |
| TC-004 | 非 JSON Content-Type | `415` | P0 |
| TC-005 | 请求体超过 32 KiB | `413` | P0 |
| TC-006 | 任一必填字段缺失、未知字段、非字符串或控制字符 | `422` | P0 |
| TC-007 | 时间无时区或非法 | `422` | P0 |
| TC-008 | optimizer 字段存在但为空 | 事件接收后进入兜底群 | P0 |
| TC-009 | username 前后有空白 | 去空白后、大小写敏感精确匹配 | P0 |
| TC-010 | username 不存在 | 兜底 `optimizer_not_found` | P0 |
| TC-011 | 有效 email 缺失 | 兜底 `optimizer_email_missing` | P0 |
| TC-012 | email 无飞书用户 | 兜底 `feishu_user_not_found` | P0 |
| TC-013 | 正常 email 解析为 open_id | 使用 `receive_id_type=open_id` 私聊 | P0 |
| TC-014 | 私聊永久失败 | 兜底群说明 `private_send_failed` | P0 |
| TC-015 | 私聊临时失败后恢复 | 按计划重试，最终只私聊 | P0 |
| TC-016 | 兜底群也发送失败 | 重试耗尽后 `dead_letter`，不标成功 | P0 |
| TC-017 | 同 key 同 payload 串行/并发重放 | 只创建一个事件，不重复正常播报 | P0 |
| TC-018 | 同 key 不同 payload | `409 idempotency_conflict` | P0 |
| TC-019 | 同一 resource_id 不同 key | 创建不同事件 | P1 |
| TC-020 | 进程在落库后重启 | worker 恢复未完成事件 | P0 |
| TC-021 | 私聊成功但本地确认丢失 | 允许极端重复，消息事件编号一致 | P1 |
| TC-022 | 文本包含 HTML/Markdown/SQL 字符 | 作为单行纯文本发送，不注入格式或 SQL | P0 |
| TC-023 | 客户端伪造 X-Real-IP | 不影响 Token 鉴权；审计只信任本机反代 | P1 |
| TC-024 | 日志、响应和 SQLite 检查 | 无 Token、App Secret、tenant token、完整 open_id | P0 |
| TC-025 | 生产兜底 canary | 群内出现一条带事件编号的联调测试消息 | P0 |
| TC-026 | 已送达事件使用同 key、同 payload 重放 | 仍返回 `202 duplicate_accepted`，不创建新事件 | P0 |
| TC-027 | Token 少于 32 字符 | 配置 fail closed；接口返回 `503` | P0 |
| TC-028 | 新旧两个有效 Token 轮换 | 任一 Token 均可鉴权成功 | P0 |
| TC-029 | RFC 3339 秒小数为 6 位/7 位 | 6 位接受，7 位返回 `422` | P1 |
| TC-030 | `resource_name` 缺失、空值或超过 255 字符 | `422` | P0 |
| TC-031 | `drama_dubbing_type` 缺失、空值或超过 64 字符 | `422` | P0 |
| TC-032 | 旧八字段请求 | `422 invalid_payload`，提示缺少两个新增字段 | P0 |
| TC-033 | 私聊与兜底消息字段 | 均按同一固定顺序展示完整十字段 | P0 |

## 回归范围

- 飞书登录、现有任务完成通知
- 截图 API Token
- FB playable API Token
- `/api/auth/status`
- 既有 Nginx 配置和静态页面
