# 测试用例

## 测试范围

Token 状态投影、刷新轮换、错误分类、发布预检/最终保护、X Auto 快照、UI 文案与现有发布回归。

## 测试数据

全部使用临时 SQLite、临时 Token 目录、HTTP/X mock 与离线预览；不使用生产 Token，不向 X 写入。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 过期但可续期 | 完整 scopes + Refresh Token | 读取账号 DTO | `status=active`，`access_token_status=expired_refreshable`，可发布 | P0 | 通过 |
| TC-002 | 过期且不可续期 | 无 Refresh Token | 读取账号 DTO | `status=expired`，不可发布，提示重新授权 | P0 | 通过 |
| TC-003 | 按需刷新 | 120 秒内到期 | 调用 `only_refresh_required` | 刷新一次、轮换完整 Token、校验身份、状态有效 | P0 | 通过 |
| TC-004 | 无需刷新 | Token 有效期充足 | 调用 `only_refresh_required` | 不调用 Token/X 用户接口 | P0 | 通过 |
| TC-005 | 明确撤销 | refresh 返回 `invalid_grant` | 自动预检 | 不写 X，账号 `revoked` | P0 | 通过 |
| TC-006 | 瞬时失败 | refresh 限流/网络失败 | 自动预检 | 不写 X，保留可续期状态并记录错误 | P0 | 通过 |
| TC-007 | 三类发布预检 | 素材/短剧/人工排期 | 调用 runner verify | 发送刷新校验和审批保护参数 | P0 | 通过 |
| TC-008 | 最终发布保护 | 队列已冻结且 Token 过期 | 执行队列 | 先刷新再进入凭证上下文；刷新失败时 X 写入为 0 | P0 | 通过 |
| TC-009 | Relay 双账号 | source/target 任一临期 | 上传与 Repost | 两个实际写入账号分别按需刷新 | P0 | 通过 |
| TC-010 | X Auto 预览 | 可续期账号 | 调用 preview | 仅读快照，不刷新、不建 Run/Task | P0 | 通过 |
| TC-011 | X Auto 实际 Run | 到期账号 | scheduler 建 Run | 刷新后冻结新账号快照 | P0 | 通过 |
| TC-012 | UI 统计 | 过期但可续期 DTO | 渲染两页 | 计入授权有效，显示可自动续期，不计入需要处理 | P1 | 通过 |
| TC-013 | 安全字段 | 任意 Token | 序列化 API/日志 | 不出现 Access/Refresh Token 原文 | P0 | 通过 |

## 回归范围

账号授权/停用/审批/语言、素材池、短剧池、人工排期、X Auto、Premium/relay、队列幂等、未知结果与静态部署契约。
