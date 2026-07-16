# 测试报告

## 1. 测试结论

截至 2026-07-16，V3 R1 已按精确 commit `79fce9e56ba70b13f09b574ba3fa20c88f522d0a` 完成生产暗发布和验收：八表 DDL/15 产品 seed、真实 MySQL 三层查询、线上登录、Campaign/Ad Set/Ad 手动 observe、动态两页、导航键级合并和 V2 生产定向回归均通过。独立评审未发现仍开放的 R1 阻塞级 P0/P1 代码缺陷；延期 QA 项在第 9 节保留。

**通过范围仅为“FB 配置 + 手动试算 + 只观察”。** scheduler、规则 enable、live pause/copy、TT、copied created_data/lineage/intent 和快照清理器仍未发布；本报告不构成自动调度或 Meta 放量结论。

## 2. 测试范围

- V3 routes/app lazy wiring、权限、same-origin JSON、方法/body 上限和动态 HTML/asset。
- 产品/optimizer/时区范围、Campaign/Ad Set/Ad adapter、候选账户两段式 `paa` 时区补查、字段能力、规则引擎和 Copy 参数。
- ads_ai repository 的八表 allowlist、读写分离、事务、CAS、DDL/seed/空表 rollback 合同。
- 数据盘路径、原子 gzip、权限、大小、余量、相对路径和 SHA-256。
- 两个动态页面、空默认值、服务端分页、XSS、响应式和锁定状态。
- exact-source code overlay 与独立 navigation 键级合并发布器。
- V2 本地冻结基线回归，以及生产页面、API 认证边界、SQLite、runner/cron 和静态文件前后比对。

## 3. 执行统计

| 类型 | 总数 | 通过 | 失败 | 阻塞/待执行 |
| --- | ---: | ---: | ---: | ---: |
| V3 unittest（含主/导航 deployer） | 139 | 139 | 0 | 0 |
| Core/查询性能专项（包含于 139） | 59 | 59 | 0 | 0 |
| Navigation deployer 子集（包含于 139） | 13 | 13 | 0 | 0 |
| V2 冻结 worktree 基线 | 146 | 143 | 0 | 3 环境阻塞 |
| Playwright 冻结 UI 视口 | 4 页面/视口组合 + 1 移动数据截图 | 5 | 0 | 0 |
| 生产 MySQL/DDL/read-after-write | 1 组 | 1 | 0 | 0 |
| 生产 route/auth/browser/manual observe | 1 组 | 1 | 0 | 0 |
| 生产 V2 定向回归（页面/API/SQLite/cron/runner/nav/自然 tick） | 1 组 | 1 | 0 | 0 |

V3 最终测试实际结果：139/139；其中 core/查询性能专项 59/59、Navigation 发布链 13/13。

V2 的 3 个阻塞并非测试断言失败：冻结 Git worktree 缺少该历史 commit 未纳入的 `features.x_accounts`，导致 import-time `ImportError`，因此该历史套件仍记为 143/146。它与另行通过的生产 V2 定向回归是两类证据，不能互相替代。

## 4. 自动化证据

主命令：

```powershell
python -m unittest discover -s tests -p "test_ad_control_v3*.py" -v
```

覆盖文件：

- `tests/test_ad_control_v3_core.py`
- `tests/test_ad_control_v3_repository.py`
- `tests/test_ad_control_v3_routes.py`
- `tests/test_ad_control_v3_ui.py`
- `tests/test_ad_control_v3_deploy.py`
- `tests/test_ad_control_v3_navigation_deploy.py`（独立 13 条专项链）

独立 R1 代码评审结论：无 P0 越权、Meta 写或 V2 破坏；P1/P2 见后文和 `sa-code-review.md`。

## 5. Playwright 证据

目录：`D:\codex\tmp\ad-control-v3-ui-final`

| 文件 | SHA-256 |
| --- | --- |
| `rule-groups-1440.png` | `EB330140497F947B9895E9C1420C7722B7DB076A8B9E514E7A083EAD73CB2459` |
| `rule-groups-390.png` | `C3C60313F3C98B1B1289802F7CC65E32BA0561C7AC9F82D4974724F650F2D1C0` |
| `execution-logs-1440.png` | `E767AAC01453C6388415A797B3CDA4BF8FCD9052CAB3E02003743B481D8129DE` |
| `execution-logs-390.png` | `5F44E67B1AB0FB2063DEFEA4BDDAC7C999D55E4AF066EA3C6D9534C4FCE3DB35` |
| `execution-logs-390-data.png` | `C87650FB8E39EEEA17A678CEF5EA413182578C42FC48909E3F8F05B591167328` |

实际确认：UTF-8 中文完整；规则页和日志页在 1440/390 均无页面级横向溢出；页面明确“计划调度器尚未发布”“仅保存草稿 + 手动试算”“启用锁定”；日志显示 manual preview/observed；console 0 Errors / 0 Warnings。

该浏览器证据使用 mock API，不证明生产 cookie/module、真实分页、真实数据库或外部写计数。

## 6. 生产验收证据

- MySQL 5.7.18；63350 `read_only=1`，63353 `ads_ai/read_only=0`。
- 八张 `ad_control_v3_*` 表已创建，118 列、28 索引、6 外键，schema hash `0b1c6e7c0528c4c50fdf2336b80cfee8650380112c45b60e7d72959489d689ef`；15 个 FB 短剧产品 seed 已由 reader 回读。
- DDL 和 seed SQL SHA-256 分别为 `deb3ae8634a1f6d3322442981f580960cab5e3c27b77dce7c8442c7e0c45e9c3`、`35789507bf289a29b5ac7972b24162f9fc1ca4a2f6a35fc58f9532b5d902c08d`；迁移证据为 `/mnt/data-disk/ai-ad-control-v3/releases/79fce9e56/migration-20260716T074440Z.json`，SHA-256 `d6a80a179b5e1bc1d38157afa284899670d3e49630d18192bcce227589d87829`。
- 最终真实查询：空时区严格 0 次 settings I/O；Campaign/Ad Set/Ad 主查询分别返回 1068/1116/2232 行，用时 4.5517/1.9697/2.2307 秒。带真实时区条件三层均在边界内结束且不误纳入；直接 `paa` 时区查找 1.2415 秒。
- 线上 admin 以 Dramawave + optimizer 582 建立一个禁用/observe 规则组，依次完成 Campaign 1081、Ad Set 1307、Ad 2607 个目标的手动试算；3 次 execution 均为 `observed`，`meta_write_count=0`。
- 生产现有数据：catalog 15、group 1、preview 3、preview target 4995、execution 3、execution target 4995、runner event 0；三份 gzip 快照均通过路径、大小和 SHA-256 回读。
- Canary 证据为 `/mnt/data-disk/ai-ad-control-v3/releases/79fce9e56/canary-20260716T080533Z.json`，SHA-256 `71ecb436dc50e18cf29cf67355c18ab5d9b3923a4f0264298dd5fca2ce2fc12b`。
- 线上浏览器验证动态规则页、日志页、三层试算、100 条对象明细、15 产品、admin 代选 optimizer、无默认业务值和启用锁定；旧 V2 规则页同时正常加载现有正式规则、账户池和日志入口。
- V2 既有 runner 在 V3 API 重启后的 15:50～16:25 连续 8 个自然 tick 均为 `skipped/no_accounts_due`，requested/success/error 均为 0；cron 唯一行和 runner hash 未变。
- `created_data` 相关表数仍为 0；旧 `ads_ai.ad_control_action_log` 行数在迁移期间保持 22。

## 7. 缺陷与评审问题

| 编号 | 级别 | 结论 | 状态 |
| --- | --- | --- | --- |
| BUG-001 | P0 | 初版 `SafeDataRoot` 的禁止祖先判断会误拒绝合法绝对路径；已改为只拒绝明确禁止目录及其后代并补测试 | 已关闭 |
| CR-001 | P0 | 初版 production service 未接线，API 会 503；已改为认证后 lazy environment build | 已关闭 |
| CR-002 | P0 | 初版 UI group ID/version、scope window 和 meta actor 合同不一致 | 已关闭并有回归 |
| CR-003 | P0 | enable 检查存在 TOCTOU 风险 | repository 使用单条 CAS/Preview guard；当前 enable 仍失败关闭 | 已关闭 |
| CR-004 | P1 | 源 product 大小写不敏感且 optimizer 热点全字段聚合超时 | 已切 dpdo/data_source 0/6、服务端字段投影、8s session/hint、9～10s socket，并保留索引等值 + BINARY exact；四处 V3 product 列 `utf8mb4_bin`；生产三层实查通过 |
| CR-005 | P1 | navigation 不应由主 overlay 整份覆盖 | 独立键级合并 deployer + 13/13，生产两份 navigation 均幂等合并且旧分组完整 | 已关闭 |
| CR-006 | P2 | 快照写入早于 MySQL 事务，失败会留孤儿文件 | 延期；无引用、无执行，当前无清理器 |
| CR-007 | P2 | 列表/详情存在可优化的 N+1 查询 | 延期；服务端分页和上限降低风险 |
| CR-008 | P2 | Preview 无通用 idempotency key | 延期；UI mutation single-flight，重复手动请求仍会生成新审计 |
| CR-009 | P0 | 三层主聚合 JOIN settings 派生表在生产只读验证中 8 秒超时 | 主聚合永久无 JOIN；非空时区筛选才对候选账户 bare/act_ 变体执行绑定、分块、`FORCE INDEX(paa)` 补查；空时区不做 settings schema probe；重复相同值合并，缺失/多 distinct 时区只阻断对应候选并持久化原因；分块查询失败、截断、超限或 deadline 才整体中止且零 Preview/Execution/快照持久化；硬限不能被构造参数放大 | 本地/服务器 139/139，生产三层空/非空时区实查通过 |

## 8. 未发布能力验证

本地自动化已证实以下失败关闭合同：

- runner 默认 disabled；即使两个 runner env 均开启也返回 scheduler 未配置；
- observe enable 返回 `runner_scheduler_not_configured`；
- live pause 返回 `live_pause_disabled`；
- live copy 返回 `copy_persistence_not_configured`，adapter copy 0 次；
- TikTok 返回 `channel_not_enabled`；
- budget/Meta status 等 roadmap 字段返回 `field_not_supported`；
- 没有 copied `created_data/lineage/intent` DDL/DML。

生产已验证两个 runner flag 均为 0、没有 V3 systemd timer/service、`runner_event=0`，三层手动 observe 的 `meta_write_count` 均为 0。Facebook adapter 本期没有 Token/Graph/mutator 路径。

## 9. 遗留风险

- 快照/MySQL 跨存储非原子且无清理器。
- 计划、额度和时区本地调度仅配置，不执行。
- 三层 live pause、任何 live copy、TT 和 created_data 关联没有发布/Canary 结论。
- `actual_cpi=0` 的 copy 预算参数目前仅影响 observe 结果；正式复制开放前应收紧为有限正数。
- service 目前将 adapter 的通用异常统一映射为可重试 503；后续应补带堆栈的服务端日志并缩小捕获范围。
- 200% 缩放、全键盘、屏幕阅读器、对比度和 Edge 仍待人工验收。

## 10. 发布建议

当前建议：保留本次 R1 生产发布，继续以“禁用规则 + 手动 observe”使用。不得启用 scheduler、规则 enable 或任何 Meta 写能力；下一阶段若开放 copy/live/TT/created_data，必须重新走需求、DDL、Canary 和回滚评审，不能沿用本次 observe 结论直接放量。
