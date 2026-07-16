# SA 代码评审

## 1. 结论

冻结 R1 独立评审未发现仍开放的 P0 越权、Meta 写入或 V2 破坏路径。最终 V3 139/139，其中 core/查询性能专项 59/59、navigation 发布链 13/13。

当前结论为**生产手动 observe R1 通过**：源 product collation、真实 MySQL 查询、DDL/read-after-write、精确部署和 V2 回归均已闭环。该结论不批准 scheduler、enable、live Meta 写、TT 或 copied created_data。

## 2. 评审范围

- `app.py` V3 lazy dispatcher 差异；
- `features/ad_control_v3/` 全部 Python、HTML、CSS、JavaScript；
- `scripts/ad_control_v3_runner.py`；
- `deploy/apply_ad_control_v3.py`；
- `deploy/apply_ad_control_v3_navigation.py`；
- `tests/test_ad_control_v3_*.py`；
- 三个审核 SQL 的合同及生产 DDL/seed/schema 回读；
- 旧 V2 文件未被 V3 runtime manifest 纳入。

## 3. 架构与安全结论

- V3 只在新前缀命中后 lazy import，旧请求不构造 service、不连接 V3 DB、不访问数据盘。
- 动态页面与 asset 受 cookie/module 保护；写请求先做 same-origin JSON 和 body 上限，再构造 service。
- 普通用户 optimizer 由服务端唯一解析；admin 目标必须 active；读范围按 optimizer，mutation 再按 owner。
- 账户范围字段递归拒绝；产品、日期、optimizer、platform 是 adapter 查询强制前置条件。
- Facebook adapter 仅 discover；没有 Token lookup、Graph client 或外部 mutator。manual preview 明确 `meta_write_count=0`。
- TT/live/copy/scheduler 由 service 与 runner 双层失败关闭；UI 的 disabled 状态不是唯一门禁。
- MySQL reader/writer factory 分离，端口/host/database/table 固定；Preview/Execution bundle 和 pointer CAS 同一事务。
- SafeDataRoot 在 mkdir 前验证路径/设备/symlink，快照限大小/空间、0600、fsync+replace、hash readback。
- exact-source deployer拒绝 drift/无关 app 差异/缺依赖，并支持幂等 apply、自动失败回滚和显式 release rollback。
- navigation 使用独立键级合并链，不由主 overlay 覆盖现场 JSON。

## 4. 问题清单

| 编号 | 级别 | 文件/位置 | 问题 | 处理 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | `service.py/get_service` | 初版未配置 production service，所有业务 API 会 503 | 认证后 lazy `build_service_from_environment`，import 无 I/O，显式三连接角色 | 已关闭 |
| CR-002 | P0 | `service.py/meta` / UI | actor、字段目录、group ID/version 合同不一致，可能导致 admin/更新/Preview 失效 | meta 返回 actor/permissions/分层目录；UI 使用 `group_id/config_version` | 已关闭 |
| CR-003 | P0 | scope estimate/preview | 初版未显式传窗口，生产扫描会失败或产生不明确日期 | 要求 `metric_window_days` 或成对 dates；UI 必填并发送 | 已关闭 |
| CR-004 | P0 | `storage.py` | 禁止祖先判断误拒绝所有 Unix 绝对路径 | 修复并记录 BUG-001，补原子写/逃逸/大小测试 | 已关闭 |
| CR-005 | P0 | enable repository | service 校验与写状态存在 TOCTOU | writer 使用 version/hash/preview/expiry 的原子 guarded update；当前 enable 仍锁定 | 已关闭 |
| CR-006 | P0 | Preview persistence | Preview、targets、execution、pointer 分步可能部分提交 | 合并 `save_preview_execution_bundle` 单事务和 target chunk/上限 | 已关闭 |
| CR-007 | P0 | Facebook aggregation | 多值上下文被 MAX 任取、group_concat 截断可能误归属 | 聚合集合、单值/歧义/截断检测，跨产品对象阻断 | 已关闭 |
| CR-008 | P0 | production env | 读写 host/port 可错配、root 可能落系统盘 | 固定生产 host，reader 63350/writer 63353、ads_ai，data root 强门禁 | 已关闭 |
| CR-009 | P1 | product SQL/DDL | 源列为 `utf8mb4_unicode_ci`，普通等值大小写不敏感；全字段聚合在 optimizer 热点超时 | 强制 dpdo/data_source 0/6；scope/Preview 服务端字段投影；8s session/hint 与 9～10s socket；保留可索引 `s.product=%s` 并追加 `BINARY` exact；四处 V3 product 列为 `utf8mb4_bin` | 自动化、生产三层实查和 DDL 回读通过 |
| CR-010 | P1 | navigation | 直接随 overlay 覆盖 navigation 会丢现场组 | 新增独立键级合并 deployer，13/13；生产两份 navigation 键级合并并重复 `unchanged` | 已关闭 |
| CR-011 | P1 | API/UI | 调度/enable/live 的配置控件易使用户误认为已发布 | 页面固定提示、启用锁定、meta `can_enable=false`，runner/service fail-close | 已关闭 |
| CR-012 | P2 | snapshot/DB | 快照先写、DB 后写，DB 失败留孤儿 | 无引用/无执行；清理器延期，保留审计 | 接受延期 |
| CR-013 | P2 | repository list/detail | optimizer/group enrichment 可能产生 N+1 | 当前服务端分页/上限缓解，后续批量 join | 接受延期 |
| CR-014 | P2 | manual Preview | 无通用 idempotency key，直接重放会生成新审计 | UI mutation single-flight；后端幂等留后续 | 接受延期 |
| CR-015 | P2 | disabled product | 产品在规则保存后被停用时的历史展示与全链路复核需真实库联调 | 保留生产联调用例 TC-014 | 待验证 |
| CR-016 | P0 | Facebook account timezone | settings 全表派生 JOIN 与三层聚合组合后生产只读查询超时 | 主聚合固定无 JOIN；候选账户 bare/act_ 变体通过绑定参数和 `FORCE INDEX(paa)` 分块补查；请求内缓存、raw/账户/分块行硬限和共享 deadline；空时区跳过 settings schema probe；缺失/多 distinct 只阻断对应候选并写原因，分块查询失败/截断/超限/deadline 才整体中止且零持久化 | 自动化和生产三层空/非空时区实查通过 |
| CR-017 | P2 | `rule_engine.py` Copy 实际 CPI | `actual_cpi=0` 目前会被视为可用并形成零预算参数 | 正式 copy 仍失败关闭；开放前要求改为有限正数并补回归 | 接受延期 |
| CR-018 | P2 | `service.py` adapter 异常 | 通用 `Exception` 统一包装成可重试 503，可能掩盖代码缺陷 | 后续缩小捕获范围并记录 `exc_info`；不影响当前失败关闭 | 接受延期 |
| CR-019 | P1 | V3 动态页 CSP | 仅在 Meta CSP 允许 QuickNav 样式 hash 时，HTTP Header CSP 与其取交集后仍会阻断动态样式 | Header 与 Meta 共享同一精确 runtime hash，并新增 route + runtime hash 自动化 | 已关闭 |
| CR-020 | P1 | 公共顶栏登出 | 脏编辑状态直接调用标准登出会先注销，随后 beforeunload 若取消会停留在已注销页面 | 登出 POST 前复用 save-in-flight/dirty 门禁；取消不调用认证接口 | 已关闭 |

## 5. 编译与自动化结果

已取得：

```text
V3 unittest: 139/139 passed
Core/query performance: 59/59 passed
Navigation deployer subset: 13/13 passed
Playwright: 1440/390, console 0 errors / 0 warnings
Production staging: 139/139 passed in 4.058s
Local final suites: 139/139 passed in 81.786s; post-documentation rerun 139/139 in 79.729s
```

主覆盖包含 Python 3.9 AST、JS syntax、route/auth、schema/adapter/rule engine、repository/DDL contract、storage、UI 和 deployer。

V2 冻结 worktree 146 条中 143 通过，3 条因缺少 `features.x_accounts` 模块在 import 阶段环境阻塞；不是断言失败，也不能算通过。另行完成的生产 V2 回归确认旧页面/API、SQLite integrity、cron、runner hash 和导航正常。

## 6. 生产复核结果

- 精确 target `79fce9e56ba70b13f09b574ba3fa20c88f522d0a` 在本地和服务器重跑 139/139，Python/JS/diff 检查通过。
- MySQL 5.7.18 三层真实查询均在安全边界内完成；空时区 settings I/O 为 0，非空时区走 `paa` 候选账户补查。
- 63353 创建八表/seed，63350 回读 118 列、28 索引、6 外键、15 产品。
- 精确 source overlay、service 重启、动态页面、生产 admin cookie/optimizer 和三层 manual observe 通过。
- 三层 execution 均 `observed/meta_write_count=0`；V3 runner/timer/cron 未发布。
- V2 文件、SQLite integrity、cron、runner hash和导航前后对比通过；未人为触发，发布后连续观察 8 个零动作 `no_accounts_due` 自然 tick。

## 7. 评审建议

批准本期“FB 配置 + 手动 observe、零 Meta 写”生产使用。P2 延期项不影响该边界，但必须在正式 copy/live 前关闭；不批准把本次结论扩张为自动扫描、规则启用或 Meta 放量。

## 8. 评审记录

- 2026-07-16：完成初版 P0/P1 审查并推动 service、scope、storage、CAS 与 aggregation 修复。
- 2026-07-16：冻结 R1 独立复审确认无 P0 越权/Meta/V2 破坏；P1 product collation 和生产证据仍为门禁。
- 2026-07-16：完成生产门禁和二次独立复审，无 P0/P1 阻塞；记录 CR-017/018 为正式 copy/live 前 P2 backlog。
- 2026-07-16：公共壳修正独立复审发现并关闭 CR-019/020；最终 `git diff --check`、JS 语法、UI+route 41/41 通过，无新增 P0/P1。
