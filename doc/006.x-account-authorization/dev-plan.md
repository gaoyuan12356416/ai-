# 开发计划

## 开发范围

在不破坏已部署 V1 和现有生产账号/Token 的前提下完成 V2：个人授权页与管理员全量页拆分、owner 联合隔离、本人单账号退出授权、X 基础资料快照、admin 同步和 legacy 数据迁移。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| V1 生产/仓库基线与现有账号盘点 | Codex | live + Git worktree + sidecar DB/Token | V1 已完成；V2 上线前重新确认 |
| V2 需求/SA/API/测试/部署文档 | Codex | `doc/006.x-account-authorization` | 已完成并按实现回读 |
| Owner schema 与幂等迁移 | Codex | `features/x_accounts/oauth_service.py` | 本地旧 schema 测试通过；生产待迁移 |
| owner actor/state/callback 归属校验 | Codex | `app.py`, client, sidecar | Sidecar 28/28、App 5/5 通过 |
| 个人列表/verify/logout 联合隔离 | Codex | 主 API + sidecar internal API | 本地回归通过 |
| 跨 owner 重授权防覆盖 | Codex | callback/upsert/账号锁 | 本地回归通过 |
| X `/users/me` 资料快照 | Codex | callback/verify/schema/DTO | 本地回归通过；真实生产账号待验证 |
| X OAuth revoke 与 pending/disconnected 状态机 | Codex | sidecar/client/app | Sidecar 28/28 通过；真实 revoke 待验证 |
| 个人授权页调整 | Codex | `static/x-accounts.html` | JS 与 Playwright 通过 |
| Admin 全量列表页与导航 | Codex | `static/x-account-list.html`, nav | JS/JSON 与 Playwright 通过 |
| Admin 全量/同步 API | Codex | `app.py`, client, sidecar | App 5/5 通过；生产待验证 |
| Legacy owner 回填与 Token 保全演练 | Codex | `scripts/backfill_x_account_owners.py`, deploy/server | Backfill 4/4 通过；生产 apply 待执行 |
| 自动化、编译、静态与安全回归 | Codex/QA | tests + required modules | 本地验证通过 |
| GitHub-first 生产部署 | Codex | GitHub/release/server | 本地门槛通过，未部署 |
| 生产真实 revoke 与迁移验收 | 用户 + Codex | `ai.yingliangads.com` | 待执行 |

## 实现顺序

1. 备份/复制 V1 SQLite 与 Token 测试数据，先实现 additive schema migration。
2. actor 全链路增加 `tenant_key`，实现 owner query primitive 和 admin query primitive。
3. 修复 BUG-004：owner list/verify/logout 均在 sidecar 服务层联合过滤，admin 使用独立路径。
4. 修复 BUG-005：callback 锁内检查已有 `x_user_id` owner，跨 owner 冲突时保持 DB/Token 不变。
5. 扩展 `/2/users/me` 请求字段与资料快照；列表只读快照。
6. 实现 confidential-client revoke 状态机：远端调用前写 `revoke_pending`，按 Access 先、Refresh 最后撤销；失败保留 Token、禁 verify、允许重试；全部成功后才删凭证并标 `disconnected`。
7. 启动时清理 `disconnected` 行对应的残留 live Token 与历史 `.disconnecting` tombstone。
8. 拆分页面/导航；admin 页面只用 admin API，admin 请求与 Nginx/API 三层 no-store，并展示刷新/同步/更新/退出时间。
9. 提供 `scripts/backfill_x_account_owners.py`：默认 dry-run、仅唯一匹配、`--require-all-resolved` 作为部署门槛。
10. 补齐自动化、并发、IDOR、pending 重试、启动清理、回填 CLI、缓存/时间显示和日志扫描。
11. 完成代码评审和完整回归后，按 GitHub-first 流程部署、迁移、重启和浏览器验收。

## 编译 / 构建命令

V2 完成后至少执行：

```powershell
python -m py_compile app.py features\x_accounts\client.py features\x_accounts\oauth_service.py
python scripts\test_x_accounts.py
python scripts\test_x_accounts_app_contract.py
node --check static\quick-nav.js
ConvertFrom-Json (Get-Content -Raw -Encoding UTF8 static\navigation.json)
git diff --check
```

还需对两个 HTML 的内联 JavaScript 做提取后 `node --check`，并在 Python 3.9 生产同版本环境重跑 X 自动化测试。文档不能预先填写为通过。

## 风险与依赖

- Legacy owner 回填依赖主库 `drama_admin_user` 对 `authorized_by_user_id` 的唯一匹配；默认 dry-run 与 `--require-all-resolved` 必须先通过，零/多匹配必须阻断 apply 和普通 owner 可见性。
- Revoke 需要真实 X API；自动化需验证 Access-first/Refresh-last、`revoke_pending`、verify 禁止、logout 重试、成功清理和启动恢复，但不能替代真实撤销。
- 跨 owner callback 在换取 Token 并识别 `/users/me` 后才能判定冲突；收到的新凭证不得覆盖或用于撤销既有 owner 的凭证。
- 生产复合 `app.py` 必须保留所有无关热修复；部署仍采用窄补丁与精确提交。
- V2 schema 为 additive，但 logout 是外部不可逆操作；回滚不能让已远端撤销的旧 Token 恢复为 active。

## 完成记录

- V1 提交 `eccabcb0d49714efa90403b140c0d2f77e5182dc` 已部署，V1 自动化 16/16 与基础设施验证通过。
- 2026-07-14：V2 本地实现完成；Sidecar 28/28、App 5/5、backfill 4/4、全量 py_compile、两页 inline JS、QuickNav、navigation JSON、diff check 和 Playwright 三路径均通过，console 0 error。生产回填、部署与真实 X revoke 待执行。
