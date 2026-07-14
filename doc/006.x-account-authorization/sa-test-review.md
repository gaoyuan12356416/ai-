# SA 测试用例评审

## 结论

V2 用例设计和本地验收通过：Sidecar 28/28、App 5/5、backfill 4/4、全量编译/静态及 Playwright 三路径通过，console 0 error。生产迁移/Token 保全、服务/API smoke、模块级查询和真实 `/2/users/me` 同步也已执行通过；真实 Cookie 浏览器、真实跨 owner OAuth 与真实 X revoke 仍待执行。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | Token 生命周期 | 只测 Access Token 未覆盖 Refresh Token 轮换 | TC-011 验证保存新 Refresh Token | Sidecar 28/28 通过 |
| STR-002 | 公网暴露 | 未验证 internal 路由无法从公网访问 | 保留 Nginx 公网 404 检查 | Sidecar旧路径404通过；生产 Nginx 待回归 |
| STR-003 | 用户文案 | “登录时间/logout”含义可能误导 | 展示授权/同步/退出授权时间；明确不退出 x.com | 已补充，本地 Playwright 通过；生产登录态待验收 |
| STR-004 | 并发 | 未覆盖 callback/verify/logout 竞争 | TC-019 统一按 x_user_id 串行 | Sidecar 28/28 通过 |
| STR-005 | Token 属主 | 未覆盖空/错误 `/users/me` ID | 保留 TC-017 | Sidecar 28/28 通过 |
| STR-006 | 配置 | 未覆盖环境删减必需 scope | 保留 TC-018 fail closed | Sidecar 28/28 通过 |
| STR-007 | Header 泄漏 | 未覆盖 revoke 30x/Basic Header | 扩展 TC-020、TC-040 | 本地通过；生产日志待扫描 |
| STR-008 | Owner 隔离 | 只测不同用户，可能漏掉相同 user ID 的跨 tenant 串权 | A/B/C 数据集 + TC-021/022 | 套件通过；生产模块级 mine=1/all=1/other=0，真实 Cookie 待验收 |
| STR-009 | IDOR | 只检查列表过滤，未覆盖按 ID 的 verify/logout | TC-023/024 断言 404 且无上游调用/数据变化 | Sidecar 28/28 通过 |
| STR-010 | Admin 边界 | 前端隐藏不等于服务端鉴权 | TC-025/026/027 同时覆盖页面、GET、POST | App 5/5、Playwright 通过 |
| STR-011 | 重授权所有权 | 未断言跨 owner 冲突时旧 Token 不被覆盖 | TC-028 比较 owner、Token hash、时间和快照 | Sidecar 28/28 通过 |
| STR-012 | Logout 状态机 | 只测成功会漏掉 Access/Refresh 任一步失败、verify 误执行与本地删除失败 | TC-033 至 TC-040 覆盖顺序、pending、失败、禁 verify、重试、幂等与本地清理 | Sidecar 28/28 通过；真实 revoke 待验收 |
| STR-013 | 资料快照/链接 | 只测完整响应会导致可选字段空值崩溃；链接缺少明确上限 | TC-030/031/032 覆盖完整、缺失及 username 1–50/超长/非法字符 | 套件及生产真实 `/users/me` 同步通过；生产登录态页面待验收 |
| STR-014 | Legacy 迁移 | 只测 schema 或唯一匹配会掩盖跨租户兜底与错误 apply 风险 | TC-042 至 TC-045 覆盖默认 dry-run、唯一/零/多匹配、`--require-all-resolved`、幂等与并发回滚 | 脚本 4/4；生产副本/live 均解析并更新 1 条，保全断言通过 |
| STR-015 | 回滚 | 远端 revoke 不可逆，恢复旧 Token 可能误标 active | TC-047 要求隔离/重授权，不恢复为 active | 已补充，待演练 |
| STR-016 | 启动恢复 | disconnected 行旁残留 live Token/tombstone 可能跨重启保留 | TC-016/041 验证启动逐账号清理且不改变 disconnected | Sidecar 28/28 通过 |
| STR-017 | 缓存与时间 | admin 数据可能被浏览器/代理缓存，页面可能遗漏刷新/同步/退出时间 | TC-046 同时验证主 API、Nginx、fetch no-store 和两页时间列 | App/Playwright 及生产未登录 API no-store 通过；真实 Cookie 页面待验收 |

## QA 修订确认

- V1 用例和历史证据保留。
- V2 用例扩展至 TC-047，并将 BUG-004、BUG-005、pending 退出状态机、启动清理、回填 CLI、缓存/时间与链接边界明确映射到回归。
- 本地 Sidecar 28/28、App 5/5、backfill 4/4、编译/静态和 Playwright，以及生产迁移/Token 保全、服务/API smoke、模块级查询和真实资料同步均已写入测试报告；真实 Cookie 浏览器、跨 owner OAuth 与真实 revoke 待执行。
