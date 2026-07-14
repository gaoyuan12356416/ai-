# 开发计划

## 开发范围

在不破坏已部署 V1/V2 和现有生产账号/Token 的前提下完成 V3：将“退出授权”改为后台本地软停用，写入 `disabled` 但保留 Token，不调用 X revoke；停用账号不可校验、不可发布，同一 owner 重新授权后可恢复为 `active`。V2 页面拆分、owner 联合隔离、资料快照、admin 同步和 legacy 数据迁移继续保留。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| V1 生产/仓库基线与现有账号盘点 | Codex | live + Git worktree + sidecar DB/Token | 已完成；V2 部署前已重新确认并备份 |
| V2 需求/SA/API/测试/部署文档 | Codex | `doc/006.x-account-authorization` | 已完成并按实现回读 |
| Owner schema 与幂等迁移 | Codex | `features/x_accounts/oauth_service.py` | 本地、生产副本与 live migration 通过 |
| owner actor/state/callback 归属校验 | Codex | `app.py`, client, sidecar | Sidecar 28/28、App 5/5 通过 |
| 个人列表/verify/logout 联合隔离 | Codex | 主 API + sidecar internal API | 本地回归通过 |
| 跨 owner 重授权防覆盖 | Codex | callback/upsert/账号锁 | 本地回归通过 |
| X `/users/me` 资料快照 | Codex | callback/verify/schema/DTO | 本地回归与真实生产同步通过 |
| V3 本地软停用与 Token 保留 | Codex | sidecar/client/app | 已实现；V3 Sidecar 28/28、App 5/5 通过 |
| 发布凭证并发门禁 | Codex | `features/x_accounts/oauth_service.py` | `publish_credentials` 持账号锁、只接受 `active`、只 yield `access_token`；实际发布未接入，未来调用必须留在 sidecar 同一 context |
| 个人授权页调整 | Codex | `static/x-accounts.html` | JS 与 Playwright 通过 |
| Admin 全量列表页与导航 | Codex | `static/x-account-list.html`, nav | JS/JSON 与 Playwright 通过 |
| Admin 全量/同步 API | Codex | `app.py`, client, sidecar | App 5/5；生产模块级 all/sync 与未登录边界通过，真实 Cookie 待验收 |
| Legacy owner 回填与 Token 保全演练 | Codex | `scripts/backfill_x_account_owners.py`, deploy/server | 自动化、生产副本与 live dry-run/apply 通过 |
| 自动化、编译、静态与安全回归 | Codex/QA | tests + required modules | 本地验证通过 |
| GitHub-first 生产部署 | Codex | GitHub/release/server | 精确提交已部署，服务/API smoke 通过 |
| 生产真实 Cookie/跨 owner OAuth/V3 软停用验收 | 用户 + Codex | `ai.yingliangads.com` | Cookie、跨 owner OAuth 与真实软停用浏览器验收待执行；不再执行真实 revoke |

## 实现顺序

1. 备份/复制 V1 SQLite 与 Token 测试数据，先实现 additive schema migration。
2. actor 全链路增加 `tenant_key`，实现 owner query primitive 和 admin query primitive。
3. 修复 BUG-004：owner list/verify/logout 均在 sidecar 服务层联合过滤，admin 使用独立路径。
4. 修复 BUG-005：callback 锁内检查已有 `x_user_id` owner，跨 owner 冲突时保持 DB/Token 不变。
5. 扩展 `/2/users/me` 请求字段与资料快照；列表只读快照。
6. V3 按用户决策废弃 V2 远端 revoke 状态机：owner 路由仍保持 `/logout` 兼容，但在账号锁内仅把记录更新为 `disabled`，清空旧错误并记录停用时间；不得读取、删除或改写 Token，不得调用 X revoke。
7. `verify_account` 对 `disabled` fail closed；DTO 明确输出 `publish_eligible=false`。新增持锁上下文 `publish_credentials`，只有重新检查后仍为 `active` 且存在 Access Token 才 yield `access_token` 字符串，不返回完整 Token 字典或 Refresh Token；实际 X Post 发布尚未接入，未来上游发布必须在同一上下文内完成，禁止“先查状态、释放锁、再取 Token/发布”。
8. 兼容历史状态：旧 `revoke_pending` 可在不读取 Token、不访问 X 的情况下收敛为 `disabled`；legacy `disconnected` 保持原语义和启动残留清理，不能伪装成可恢复的软停用账号；`disabled` 绝不进入 disconnected Token 清理。
9. 移除 owner pending 对新 authorize/callback 的门禁；同一 owner 重新授权同一账号可写入新 Token 并恢复 `active`，跨 owner 防覆盖继续生效。
10. 拆分页面/导航；admin 页面只用 admin API，admin 请求与 Nginx/API 三层 no-store，并展示刷新、同步、更新和停用时间。个人页明确“停用保留 Token、不调用 X、停止后台发布”。
11. 提供 `scripts/backfill_x_account_owners.py`：默认 dry-run、仅唯一匹配、`--require-all-resolved` 作为部署门槛。
12. 补齐自动化、并发、IDOR、软停用幂等、Token 字节保全、legacy pending 收敛、disabled 校验/发布拒绝、发布锁、重新授权、启动清理、回填 CLI、缓存/时间显示和日志扫描。
13. 完成代码评审和完整回归后，按 GitHub-first 流程部署、迁移、窄重启和浏览器验收。

## 编译 / 构建命令

V3 完成后至少执行：

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
- 本地软停用不会使 X 侧 Token 失效，这是 V3 的明确业务决策。安全边界由 owner 鉴权、`disabled` 终态和发布入口 fail closed 共同保证；实际发布尚未接入，任何未来发布代码都必须使用持锁 `publish_credentials`，且只能在同一 context 内使用其 yield 的 `access_token`，不得直接读取 Token 文件。
- `publish_credentials` 必须覆盖“检查 active -> 读取 Token -> 完成上游发布”的完整临界区，否则停用与发布之间存在 TOCTOU 竞争；敏感输出只允许 `access_token`，实际发布尚未接入，未来发布必须留在 sidecar 同一 context。只有精确状态 `active` 可发布，`refresh_required`、`revoke_pending`、`disabled`、`disconnected` 及其他状态全部拒绝。
- 旧 `revoke_pending` 仅作为迁移输入保留并可收敛为 `disabled`；legacy `disconnected` 仍代表旧流程已解除授权/删除凭证，不能批量恢复或与 `disabled` 合并。
- 跨 owner callback 在换取 Token 并识别 `/users/me` 后才能判定冲突；收到的新凭证不得覆盖或用于撤销既有 owner 的凭证。
- 生产复合 `app.py` 必须保留所有无关热修复；部署仍采用窄补丁与精确提交。
- V3 不执行新的外部不可逆 revoke；但代码回滚不得把 `disabled` 自动改回 `active`。legacy `disconnected` 仍按历史不可恢复边界处理。

## 完成记录

- V1 提交 `eccabcb0d49714efa90403b140c0d2f77e5182dc` 已部署，V1 自动化 16/16 与基础设施验证通过。
- 2026-07-14：V2 本地实现完成；Sidecar 28/28、App 5/5、backfill 4/4、全量 py_compile、两页 inline JS、QuickNav、navigation JSON、diff check 和 Playwright 三路径均通过，console 0 error。
- 2026-07-14：精确提交 `e00bd30adb466f92b38f218bfb7f288ea7ff0a69` 已部署到 `/root/releases/ai-x-account-authorization-e00bd30adb466`；备份位于 `/root/backups/drama_material_service/20260714T070906Z-x-accounts-v2-e00bd30`。副本演练与 live owner 回填各完成 1 条，原 row/x_user_id/status 与 Token SHA-256/`0600` 保持，服务/API smoke、模块级 mine/all/other 隔离和真实 `/2/users/me` 同步通过。
- 2026-07-14：真实 V2 logout 首次远端调用失败后，用户明确决定采用 V3 本地软停用。旧 Access-first/Refresh-last revoke、owner pending 门禁和“成功后删除 Token”方案自此被取代，不再作为当前实现或生产验收目标。
- 2026-07-14：V3 代码与用例完成；`python scripts/test_x_accounts.py` 28/28、`python scripts/test_x_accounts_app_contract.py` 5/5 通过。测试总数相较 V2 变化是因为远端 revoke 用例被删除，并由软停用、Token 保全、legacy pending/disconnected、`publish_credentials` 锁、延迟 callback 最后写入和重新授权用例替代。
- 当前不标记 V3 全量生产验收：真实 Feishu Cookie 浏览器、真实跨 owner OAuth callback 与生产软停用页面流程尚未执行；真实 X revoke 已退出验收范围。
