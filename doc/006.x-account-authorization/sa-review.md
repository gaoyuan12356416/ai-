# SA 评审意见

## 结论

有条件通过。采用独立 X OAuth sidecar 作为凭证唯一持有方；AI 主后台只能通过 loopback internal API读取脱敏数据。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | 高 | OAuth callback | 直接把 callback迁入主后台会重复保存 Client Secret并与现有路由冲突 | 保留 `/x-oauth/callback` sidecar 所有权 | 已采纳 |
| SA-002 | 高 | Nginx | 当前 `/x-oauth/` 宽泛代理会把新增 internal API暴露公网 | 改为 callback/health 精确 location，其他路径返回 404 | 已采纳 |
| SA-003 | 高 | Token 存储 | 主业务 SQLite 当前为 `0644`，不能存 X Token | Token 独立文件 `0600`，sidecar DB `0600` 仅存元数据 | 已采纳 |
| SA-004 | 中 | 权限 | 普通 API Token 可能继承模块权限 | X API统一使用 `_require_cookie_module('x_accounts')` | 已采纳 |
| SA-005 | 中 | 状态 | 把 Access Token 到期直接显示为授权失效会误导 | 有 Refresh Token时显示 `refresh_required` | 已采纳 |
| SA-006 | 中 | 导航 | 只改 quick-nav 默认值会被线上 navigation.json覆盖 | 同步 quick-nav、navigation.json和公网副本 | 已采纳 |
| SA-007 | 高 | Callback日志 | 默认 Nginx/requestline 会记录 code/state | callback关闭 access log，sidecar日志只记 path | 已采纳 |
| SA-008 | 中 | POST安全 | Cookie写操作缺少显式同源约束 | 强制 JSON并校验 Origin/Referer | 已采纳 |
| SA-009 | 中 | 刷新并发 | 并发 Refresh Token轮换可能互相作废 | sidecar按账号串行校验/刷新 | 已采纳 |
| SA-010 | 中 | API契约 | 错误正文透传、时间格式和缓存未锁定 | 白名单错误、UTC Z、no-store | 已采纳 |

## 决策记录

- 回调地址保持 `https://ai.yingliangads.com/x-oauth/callback`。
- sidecar 并入 GitHub 维护的 AI 后台仓库，但继续作为独立 systemd 服务运行。
- 本期只实现授权、列表和主动校验，不实现删除/撤销/发帖。

## PM 修订确认

上述建议均已写入 requirements.md。
