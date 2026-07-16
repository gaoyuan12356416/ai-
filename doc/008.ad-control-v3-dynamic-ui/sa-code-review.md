# SA 代码评审

## 1. 结论

冻结 R1 独立评审未发现仍开放的 P0 越权、Meta 写入或 V2 破坏路径。最终 V3 137/137，其中 core/查询性能专项 57/57、navigation 发布链 13/13。

当前结论为**通过本地代码评审、生产有条件**：可进入 Git 提交；源 product collation 的代码/DDL修复已闭环，但真实 MySQL query plan/DDL/read-after-write、生产部署和 V2 回归完成前，不允许给出生产通过结论。

## 2. 评审范围

- `app.py` V3 lazy dispatcher 差异；
- `features/ad_control_v3/` 全部 Python、HTML、CSS、JavaScript；
- `scripts/ad_control_v3_runner.py`；
- `deploy/apply_ad_control_v3.py`；
- `deploy/apply_ad_control_v3_navigation.py`；
- `tests/test_ad_control_v3_*.py`；
- 三个审核 SQL 的合同（未执行生产 DDL）；
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
| CR-009 | P1 | product SQL/DDL | 源列为 `utf8mb4_unicode_ci`，普通等值大小写不敏感；全字段聚合在 optimizer 热点超时 | 强制 dpdo/data_source 0/6；scope/Preview 服务端字段投影；8s session/hint 与 9～10s socket；保留可索引 `s.product=%s` 并追加 `BINARY` exact；四处 V3 product 列为 `utf8mb4_bin` | 已关闭；生产 EXPLAIN/DDL 待验 |
| CR-010 | P1 | navigation | 直接随 overlay 覆盖 navigation 会丢现场组 | 新增独立键级合并 deployer，13/13 | 已关闭，生产待执行 |
| CR-011 | P1 | API/UI | 调度/enable/live 的配置控件易使用户误认为已发布 | 页面固定提示、启用锁定、meta `can_enable=false`，runner/service fail-close | 已关闭 |
| CR-012 | P2 | snapshot/DB | 快照先写、DB 后写，DB 失败留孤儿 | 无引用/无执行；清理器延期，保留审计 | 接受延期 |
| CR-013 | P2 | repository list/detail | optimizer/group enrichment 可能产生 N+1 | 当前服务端分页/上限缓解，后续批量 join | 接受延期 |
| CR-014 | P2 | manual Preview | 无通用 idempotency key，直接重放会生成新审计 | UI mutation single-flight；后端幂等留后续 | 接受延期 |
| CR-015 | P2 | disabled product | 产品在规则保存后被停用时的历史展示与全链路复核需真实库联调 | 保留生产联调用例 TC-014 | 待验证 |
| CR-016 | P0 | Facebook account timezone | settings 全表派生 JOIN 与三层聚合组合后生产只读查询超时 | 主聚合固定无 JOIN；候选账户 bare/act_ 变体通过绑定参数和 `FORCE INDEX(paa)` 分块补查；请求内缓存、raw/账户/分块行上限和共享 deadline；缺失/多 distinct/查询失败均阻断且无部分 Preview | 本地关闭；生产三层实查待验 |

## 5. 编译与自动化结果

已取得：

```text
V3 unittest: 137/137 passed
Core/query performance: 57/57 passed
Navigation deployer subset: 13/13 passed
Playwright: 1440/390, console 0 errors / 0 warnings
```

主覆盖包含 Python 3.9 AST、JS syntax、route/auth、schema/adapter/rule engine、repository/DDL contract、storage、UI 和 deployer。

V2 冻结 worktree 146 条中 143 通过，3 条因缺少 `features.x_accounts` 模块在 import 阶段环境阻塞；不是断言失败，也不能算通过。最终 target/生产完整模块必须重跑。

## 6. 生产前复核项

- 在精确 target commit 上重跑全部 137 条与 `git diff --check`。
- 对真实 MySQL 5.7.18 执行 product query `EXPLAIN`，确认索引等值前置和 binary 精确语义同时成立。
- 63353 建八表/seed，63350 schema/15 产品/read-after-write 回读。
- 最终 live app hash 与 source commit blob 一致后才能 apply overlay。
- 线上真实 cookie/module/optimizer 权限和三层 manual observe。
- Token/Graph/Meta 写 0，V3 runner/timer 未发布。
- V2 文件、SQLite、cron、runner、自然 tick 前后比对。

## 7. 评审建议

在生产门禁完成前，只批准 Git/staging，不批准对用户宣称上线完成。P2 延期项不影响本期“手动 observe、零 Meta 写”安全目标，但必须保留在下一阶段 backlog，不能在文档中消失。

## 8. 评审记录

- 2026-07-16：完成初版 P0/P1 审查并推动 service、scope、storage、CAS 与 aggregation 修复。
- 2026-07-16：冻结 R1 独立复审确认无 P0 越权/Meta/V2 破坏；P1 product collation 和生产证据仍为门禁。
