# SA 代码评审

## 结论

通过。两轮只读评审确认所有已发现 P0/P1均已修复，最终复核未发现仍然确定的 P0/P1。

## 评审范围

- `app.py` 的权限、Cookie API、同源校验、错误白名单与审计。
- `features/x_accounts/client.py`、`oauth_service.py` 的 OAuth/PKCE、Token、SQLite、并发与日志。
- `static/x-accounts.html`、导航、Nginx、systemd和配置模板。
- 需求、API、测试与部署文档。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | Nginx | 精确列表未代理，callback会记录 code/state | 增 exact location，callback关闭 access log | 已修复 |
| CR-002 | P1 | app/client | 上游错误正文直出且状态码丢失 | 白名单 code/status/固定文案 | 已修复 |
| CR-003 | P1 | app | Cookie POST缺少显式同源约束 | 强制 JSON并校验 Origin/Referer | 已修复 |
| CR-004 | P1 | sidecar | callback/verify未共享账号锁 | 统一按 `x_user_id` 串行，锁内重读 | 已修复 |
| CR-005 | P1 | sidecar | `/users/me` 未核对 Token属主 | 非空且严格匹配 `x_user_id` | 已修复 |
| CR-006 | P1 | sidecar | 环境变量可删减必需 scope | 固定 `REQUIRED_SCOPES` 并 fail closed | 已修复 |
| CR-007 | P2 | urllib | 30x可能转发 Authorization | client和 sidecar均禁自动跳转 | 已修复 |
| CR-008 | P2 | sidecar | 审计失败可能反转已成功结果 | 审计 best-effort，核心结果独立 | 已修复 |
| CR-009 | P2 | UI/API | UTC契约、缓存、缺失 scope展示不足 | UTC Z、no-store、红色缺失标签 | 已修复 |

## 编译 / 验证结果

- `python scripts/test_x_accounts.py`：16/16通过。
- `python -m py_compile`：主 app、sidecar/client及规定回归模块通过。
- `node --check`：QuickNav与页面内联 JS通过。
- `ConvertFrom-Json static/navigation.json`：通过。
- `git diff --check`：通过。
