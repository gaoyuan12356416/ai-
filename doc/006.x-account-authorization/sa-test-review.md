# SA 测试用例评审

## 结论

V1/V2 历史用例与证据继续保留。V3 测试设计已按用户最终决策改为本地软停用：`disabled` 保留 Token、不调用 X，不可校验/发布；旧 pending 可收敛，legacy disconnected 保持；实际发布尚未接入，未来发布必须使用持锁 `publish_credentials`，且敏感凭证只 yield `access_token`。V3 Sidecar 28/28、App 5/5 通过。生产真实 Cookie、跨 owner OAuth 与软停用页面流程仍待执行；真实 X revoke 已退出验收范围。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | Token 生命周期 | 只测 Access Token 未覆盖 Refresh Token 轮换 | TC-011 验证保存新 Refresh Token | Sidecar 28/28 通过 |
| STR-002 | 公网暴露 | 未验证 internal 路由无法从公网访问 | 保留 Nginx 公网 404 检查 | Sidecar旧路径404通过；生产 Nginx 待回归 |
| STR-003 | 用户文案 | “登录时间/logout”含义可能误导，且 V2 删除 Token 文案与 V3 决策冲突 | 展示授权/同步/停用时间；明确仅后台停用、Token 保留、不调用 X、不退出 x.com | V3 文案已补充；生产登录态待验收 |
| STR-004 | 并发 | 未覆盖 callback/verify/logout 竞争 | TC-019 统一按 x_user_id 串行 | Sidecar 28/28 通过 |
| STR-005 | Token 属主 | 未覆盖空/错误 `/users/me` ID | 保留 TC-017 | Sidecar 28/28 通过 |
| STR-006 | 配置 | 未覆盖环境删减必需 scope | 保留 TC-018 fail closed | Sidecar 28/28 通过 |
| STR-007 | Header 泄漏 | OAuth token/user 请求仍需防止 30x 转发 Authorization；V3 已无 revoke Header 路径 | 保留 token/user 禁重定向与日志扫描；删除废弃 revoke Header 用例 | V3 token/user 回归通过；生产日志待扫描 |
| STR-008 | Owner 隔离 | 只测不同用户，可能漏掉相同 user ID 的跨 tenant 串权 | A/B/C 数据集 + TC-021/022 | 套件通过；生产模块级 mine=1/all=1/other=0，真实 Cookie 待验收 |
| STR-009 | IDOR | 只检查列表过滤，未覆盖按 ID 的 verify/logout | TC-023/024 断言 404 且无上游调用/数据变化 | Sidecar 28/28 通过 |
| STR-010 | Admin 边界 | 前端隐藏不等于服务端鉴权 | TC-025/026/027 同时覆盖页面、GET、POST | App 5/5、Playwright 通过 |
| STR-011 | 重授权所有权 | 未断言跨 owner 冲突时旧 Token 不被覆盖 | TC-028 比较 owner、Token hash、时间和快照 | Sidecar 28/28 通过 |
| STR-012 | 本地软停用 | 若沿用 V2 测试会遗漏“不得读/删 Token、不得调用 X”、幂等、metadata 保留和 disabled 禁校验 | V3 用例断言 DB 仅写 `disabled`，Token 文件逐字节不变，Token/X/delete mock 均零调用；重复停用幂等，verify 返回 `x_account_disabled` | Sidecar 28/28 通过 |
| STR-013 | 资料快照/链接 | 只测完整响应会导致可选字段空值崩溃；链接缺少明确上限 | TC-030/031/032 覆盖完整、缺失及 username 1–50/超长/非法字符 | 套件及生产真实 `/users/me` 同步通过；生产登录态页面待验收 |
| STR-014 | Legacy 迁移 | 只测 schema 或唯一匹配会掩盖跨租户兜底与错误 apply 风险 | TC-042 至 TC-045 覆盖默认 dry-run、唯一/零/多匹配、`--require-all-resolved`、幂等与并发回滚 | 脚本 4/4；生产副本/live 均解析并更新 1 条，保全断言通过 |
| STR-015 | 回滚 | V3 虽不执行新 revoke，但代码回滚可能把保留 Token 的 disabled 记录误投影为 active | 回滚必须保留 DB 状态并验证 disabled 不自动激活；legacy disconnected 继续禁止恢复旧 Token 为 active | 已纳入 V3 回滚门槛，生产待演练 |
| STR-016 | 启动恢复 | disconnected 残留仍需清理，但误把 disabled 纳入 cleanup 会删除 V3 保留 Token | 验证 cleanup 只选择 `status='disconnected'`，disabled Token 重启前后 hash 不变 | V3 Token 保全回归通过；生产重启 hash 待验收 |
| STR-017 | 缓存与时间 | admin 数据可能被浏览器/代理缓存，页面可能遗漏刷新/同步/退出时间 | TC-046 同时验证主 API、Nginx、fetch no-store 和两页时间列 | App/Playwright 及生产未登录 API no-store 通过；真实 Cookie 页面待验收 |
| STR-018 | 发布 TOCTOU | 只测 `status == active` 会漏掉检查后停用、发布仍继续的竞争 | 用并发用例证明 `publish_credentials` 从锁内重查到 context 退出持续持有账号锁；实际发布未接入，未来上游调用必须在 sidecar 同一 context 内只使用 yield 的 `access_token` | V3 并发门禁回归通过 |
| STR-019 | 发布状态白名单 | 仅排除 disabled 可能让 pending/disconnected/error/refresh_required 被错误选中 | `publish_credentials` 只接受精确 `active` 且 Access Token 非空，敏感凭证只 yield `access_token`；其他状态返回白名单错误且不泄露完整 Token/Refresh Token | V3 回归通过；实际发布尚未接入 |
| STR-020 | Legacy 收敛 | 生产旧 `revoke_pending` 可能 Token 不可读；若仍访问 X 会继续失败 | pending logout 不读 Token、不调用 X，直接写 disabled、清旧错误；legacy disconnected 原样保留 | V3 回归通过 |
| STR-021 | 重新授权 | Token 保留后若 callback 仍被 pending/disabled 门禁阻断，账号无法恢复 | 同 owner 重授权 disabled 账号恢复 active；跨 owner 仍拒绝且原 Token/owner 不变 | V3 回归通过 |

## QA 修订确认

- V1/V2 用例和历史证据保留，但 V2 远端 revoke 顺序、失败重试和真实 revoke 验收已被 V3 用户决策废弃，不得继续作为当前阻塞项。
- V3 当前 `python scripts/test_x_accounts.py` 28/28、`python scripts/test_x_accounts_app_contract.py` 5/5 通过。28 个 Sidecar 用例已用软停用、Token 保全、legacy pending/disconnected、disabled fail-closed、重新授权、延迟 callback 最后写入和 publish context 锁覆盖取代废弃的 revoke 用例。
- 实际 X Post 发布尚未接入。未来新增发布实现时，测试必须证明真实上游发布调用发生在 `publish_credentials` 同一 context 内，并只使用 helper yield 的 `access_token`；把字符串带出上下文或直读完整 Token 均不算通过。
- 生产迁移/Token 保全、服务/API smoke、模块级查询和真实资料同步的 V2 证据继续有效；V3 仍需真实 Cookie 浏览器、跨 owner OAuth、生产软停用以及停用前后 Token hash/状态/日志验证。
