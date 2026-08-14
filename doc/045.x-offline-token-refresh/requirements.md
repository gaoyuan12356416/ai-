# 045.x-offline-token-refresh 需求与技术设计

## 背景

X OAuth 2.0 Access Token 默认仅约两小时有效，但账号已授权 `offline.access` 且服务端保存 Refresh Token 时，授权关系仍可通过刷新继续使用。当前列表把短期 Access Token 到期直接显示为“已到期/需要处理”，同时自动发布预检只读取快照、最终发布又拒绝刷新，导致所有账号在两小时后显示异常并使自然发布失败。

## 目标

- 将“短期 Access Token 到期但可自动续期”与“Refresh Token 不可用、必须重新授权”分开。
- 自动模板、素材池、短剧池以及人工已排期任务在真正执行前按账号自动续期。
- 在 X 写入前再次做最小续期保护，避免预检到上传之间 Token 到期。
- 保持账号停用、发布审批、语言路由、会员长视频、队列幂等和未知结果保护不变。

## 范围

### 包含

- 账号安全 DTO 与个人/管理员列表状态文案。
- Sidecar Token 状态投影、Refresh Token 轮换、自动发布预检和最终发布保护。
- X Auto 实际建 Run 前刷新并冻结新账号快照；预览仍只读。
- 单元、契约、UI 静态检查、部署与回滚证据。

### 不包含

- 不改变 X 平台 Token 生命周期，也不承诺 Refresh Token 永久有效。
- 不恢复已停用、已撤销、缺少 Token 或权限不完整的账号。
- 不手动触发 Run Now、发布 canary 或额外真实 X Post。
- 不重写历史 Run、Task、Queue、Log 或 Post。

## 用户故事 / 业务规则

1. 账号具备完整必需 scope、Token 文件、`offline.access` 和 Refresh Token 时，即使 Access Token 已到期，授权状态仍显示“有效”，并标注“Access Token 已到期，可自动续期”。
2. Access Token 到期且无法续期时，账号显示“需重新授权”，不可进入新发布任务。
3. 自动任务预检只在 Token 缺失或将在 120 秒内到期时调用 Token 刷新接口；无需刷新时不得轮换 Token，身份/会员校验仍可读取 `/2/users/me`。
4. Refresh Token 可能轮换，成功刷新必须原子替换完整 Token 文件并保持服务账号所有权和 `0600` 模式。
5. `invalid_grant`、Refresh Token 缺失或明确撤销才进入需重新授权状态；限流、网络故障等瞬时错误仅使本次任务安全失败并保留后续自然重试机会。
6. 最终 X 写入仍必须位于账号锁覆盖的 `publish_credentials(...)` 上下文中；上下文只暴露 Access Token。
7. X Auto 预览不得刷新 Token、创建任务或保留素材；实际自动/人工确认建 Run 时才刷新并冻结账号快照。

## 交互与流程

账号列表读取本地安全快照 → 判断授权是否可续期 → 展示授权有效/需处理及 Access Token 子状态。自动执行为：到期任务触发 → 各目标账号按需刷新并校验身份 → 冻结队列/任务 → 上传前再次按需刷新 → 在账号锁内读取当前 Access Token 并写入 X → 持久化结果。

## 技术设计

### 影响模块

- `features/x_accounts/oauth_service.py`
- `scripts/x_post_daily_runner.py`
- `features/x_auto_posts/service.py`
- `static/x-account-list.html`
- `static/x-accounts.html`
- 对应 X 单元、契约与 UI 测试

### 数据结构

无数据库迁移。现有账号 DTO 新增安全字段：`access_token_expired`、`refresh_token_available`、`authorization_refreshable`、`access_token_status`；不返回任何 Token 内容。

### API / 接口

- 现有账号查询接口兼容新增字段，不删除旧字段。
- `/internal/posts/accounts/{id}/verify` 接受与 X Auto 相同的按需刷新布尔保护参数。
- 现有 X Auto verify 路由保持不变，实际 Run 建立流程改为调用按需刷新。

### 异常与边界

- 无 Access Token 但有可用 Refresh Token：按需刷新后继续。
- Access Token 将在 120 秒内到期：预先刷新。
- Refresh Token 缺失/撤销：不写 X，账号转为 `revoked`，提示重新授权。
- 瞬时刷新失败：不写 X、不错误改成撤销，当前执行按已知失败处理。
- 停用、未批准、语言不匹配、会员降级：继续 fail closed。

## 验收标准

- 生产 17 个具备 `offline.access` 和 Refresh Token 的账号不再因两小时 Access Token 到期被统计为“需要处理”。
- 账号列表明确展示自动续期能力和短期 Token 状态。
- 三类自然自动发布和 X Auto 执行均在预检及最终写入前具备按需刷新保护。
- Refresh 失败时 X 写入 mock 调用数为 0；成功刷新只轮换一次且身份匹配。
- 完整 X 回归、编译、JS 语法、离线预览和生产健康检查通过。
- 部署验证不创建测试 Run、Queue 或真实 Post，历史账本不变。

## 风险与待确认

- Refresh Token 仍可能被用户撤销或因重新授权轮换；此时必须重新授权，并禁止恢复旧备份 Token。
- 生产自然任务在部署窗口暂停，部署后按原 enabled/active 状态恢复；若错过时段，只由既有 grace/idempotency 规则处理，不人工补发。

## 变更记录

- 2026-08-14：需求确认并进入修复，生产发布触发器已按原状态留档后暂时停止。
- 2026-08-14：实现与 667 项 X 回归完成，进入 GitHub-first 部署准备。
