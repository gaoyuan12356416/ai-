# 测试用例

## 测试范围

权限、OAuth state/PKCE、多账号 upsert、Token 隔离、列表状态、主动校验、导航与生产回调。

## 测试数据

- 本地临时 SQLite/Token 目录。
- Mock X token/user 响应。
- 生产真实 X 账号由用户授权。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 未登录访问列表 | 无 Cookie | GET `/api/x-accounts` | 401 | P0 | 待执行 |
| TC-002 | 无模块权限 | 普通用户 | GET/POST X API | 403 | P0 | 待执行 |
| TC-003 | API Token 尝试授权 | 有 API Token | POST authorize | 403 cookie_auth_required | P0 | 待执行 |
| TC-004 | 发起授权 | 有权限 Cookie | POST authorize | URL含五项scope、S256、正确 callback | P0 | 自动化通过 |
| TC-005 | state 错误/过期/重放 | 构造异常 callback | 访问 callback | 拒绝且不写 Token | P0 | 自动化通过 |
| TC-006 | 首次授权 | Mock token/user成功 | callback | 新增1账号，Token文件0600 | P0 | 自动化通过 |
| TC-007 | 重复授权 | 同一 x_user_id | 再次 callback | 仍为1行，最近授权/更新时间变化 | P0 | 自动化通过 |
| TC-008 | 多账号授权 | 两个 x_user_id | 两次 callback | 列表2行 | P0 | 自动化通过 |
| TC-009 | scope缺失 | token少一项scope | callback | `scope_missing`并列出缺失权限 | P0 | 自动化通过 |
| TC-010 | Access Token 到期 | 有 Refresh Token | 列表 | `refresh_required` | P1 | 自动化通过 |
| TC-011 | 主动校验/刷新 | token过期 | POST verify | 刷新、轮换Token、状态active | P0 | 自动化通过 |
| TC-012 | 授权撤销 | refresh invalid_grant | POST verify | 状态revoked，错误脱敏 | P0 | 自动化通过 |
| TC-013 | 内部接口鉴权 | 无/错 internal token | 访问 `/internal/*` | 403 | P0 | 自动化通过 |
| TC-014 | 敏感数据泄漏扫描 | 完成授权 | 搜索API/HTML/log | 无Secret/Token/code/verifier | P0 | 本地通过，生产待验 |
| TC-015 | 导航与页面 | 有/无权限用户 | 浏览器打开后台 | 可见性、登录门、权限门正确 | P0 | 待执行 |
| TC-016 | 服务重启恢复 | 已授权 | 重启两个服务 | 账号列表仍存在 | P1 | 待执行 |
| TC-017 | Token属主不一致 | `/users/me` 空或错误ID | POST verify | `x_identity_mismatch`，不标active | P0 | 自动化通过 |
| TC-018 | 必需 scope配置被删减 | env缺 `media.write` | 启动/发起授权 | fail closed，不显示配置完整 | P0 | 自动化通过 |
| TC-019 | 并发刷新/重新授权 | 两个并发请求 | verify/callback | 只刷新一次，最终Token与元数据一致 | P0 | 自动化通过 |
| TC-020 | 上游跨域跳转 | 30x目标收集Header | client/X请求 | 拒绝跳转，不转发Authorization | P1 | 自动化通过 |

## 回归范围

- Feishu 登录、用户权限管理、公共导航。
- 主后台 health/auth API。
- 原 X callback/health 地址。
- 现有 drama/ad-control/playable API仅做冒烟，不改业务逻辑。
