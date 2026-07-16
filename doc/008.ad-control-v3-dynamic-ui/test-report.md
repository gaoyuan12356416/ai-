# 测试报告

## 1. 测试结论

截至 2026-07-16 文档收口：V3 冻结实现的本地自动化和 mock Playwright 通过，独立 R1 评审未发现 P0 越权、Meta 写入或 V2 破坏路径。**生产部署、八表 DDL/seed、真实 MySQL 查询计划、线上登录/浏览器、三层真实数据手动 observe 和 V2 发布后回归尚未执行，因此当前不能给出“生产通过/可放量”结论。**

本期允许的下一步仅为：最终 commit/push、精确 staging、数据库/route dark 检查和手动 observe。scheduler、规则 enable、live pause/copy、TT、copied created_data 和快照清理器不得发布。

## 2. 测试范围

- V3 routes/app lazy wiring、权限、same-origin JSON、方法/body 上限和动态 HTML/asset。
- 产品/optimizer/时区范围、Campaign/Ad Set/Ad adapter、字段能力、规则引擎和 Copy 参数。
- ads_ai repository 的八表 allowlist、读写分离、事务、CAS、DDL/seed/空表 rollback 合同。
- 数据盘路径、原子 gzip、权限、大小、余量、相对路径和 SHA-256。
- 两个动态页面、空默认值、服务端分页、XSS、响应式和锁定状态。
- exact-source code overlay 与独立 navigation 键级合并发布器。
- V2 本地冻结基线回归；生产 V2 前后比对待执行。

## 3. 执行统计

| 类型 | 总数 | 通过 | 失败 | 阻塞/待执行 |
| --- | ---: | ---: | ---: | ---: |
| V3 unittest（含主/导航 deployer） | 132 | 132 | 0 | 0 |
| Core/查询性能专项（包含于 132） | 52 | 52 | 0 | 0 |
| Product/安全相关子集（包含于 132） | 56 | 56 | 0 | 0 |
| Navigation deployer 子集（包含于 132） | 13 | 13 | 0 | 0 |
| V2 冻结 worktree 基线 | 146 | 143 | 0 | 3 环境阻塞 |
| Playwright 冻结 UI 视口 | 4 页面/视口组合 + 1 移动数据截图 | 5 | 0 | 0 |
| 生产 MySQL/DDL/read-after-write | 1 组 | 0 | 0 | 1 待执行 |
| 生产 route/auth/browser/manual observe | 1 组 | 0 | 0 | 1 待执行 |
| 生产 V2 发布前后回归/自然 tick | 1 组 | 0 | 0 | 1 待执行 |

V3 最终测试实际结果：132/132；其中 core/查询性能专项 52/52、product/安全相关子集 56/56、Navigation 发布链 13/13。

V2 的 3 个阻塞并非测试断言失败：冻结 Git worktree 缺少生产/用户工作树中未纳入该 commit 的 `features.x_accounts`，导致 import-time `ImportError`。这不能被记为通过；最终 target commit 与生产完整模块上必须重跑。

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

## 6. 生产只读核实

已只读核实但尚未执行 V3 写入：

- MySQL 5.7.18；
- 63350 `read_only=1`；
- 63353 当前数据库 `ads_ai`、`read_only=0`；
- `ads_ai.ad_control_v3_*` 当前表数为 0；
- 源 `product` 字段真实 collation 为 `utf8mb4_unicode_ci`，需用可索引等值前置条件并额外做 binary 精确复核；V3 product 列采用 binary collation；
- query plan、DDL/schema hash、seed 和 read-after-write 复制可见性仍是发布门禁。

以上只读事实不代表数据库迁移通过。

## 7. 缺陷与评审问题

| 编号 | 级别 | 结论 | 状态 |
| --- | --- | --- | --- |
| BUG-001 | P0 | 初版 `SafeDataRoot` 的禁止祖先判断会误拒绝合法绝对路径；已改为只拒绝明确禁止目录及其后代并补测试 | 已关闭 |
| CR-001 | P0 | 初版 production service 未接线，API 会 503；已改为认证后 lazy environment build | 已关闭 |
| CR-002 | P0 | 初版 UI group ID/version、scope window 和 meta actor 合同不一致 | 已关闭并有回归 |
| CR-003 | P0 | enable 检查存在 TOCTOU 风险 | repository 使用单条 CAS/Preview guard；当前 enable 仍失败关闭 | 已关闭 |
| CR-004 | P1 | 源 product 大小写不敏感且 optimizer 热点全字段聚合超时 | 已切 dpdo/data_source 0/6、服务端字段投影、8s session/hint、9～10s socket，并保留索引等值 + BINARY exact；四处 V3 product 列 `utf8mb4_bin`，生产 EXPLAIN/DDL 待验 |
| CR-005 | P1 | navigation 不应由主 overlay 整份覆盖 | 独立键级合并 deployer + 13/13 | 已关闭（生产待执行） |
| CR-006 | P2 | 快照写入早于 MySQL 事务，失败会留孤儿文件 | 延期；无引用、无执行，当前无清理器 |
| CR-007 | P2 | 列表/详情存在可优化的 N+1 查询 | 延期；服务端分页和上限降低风险 |
| CR-008 | P2 | Preview 无通用 idempotency key | 延期；UI mutation single-flight，重复手动请求仍会生成新审计 |

## 8. 未发布能力验证

本地自动化已证实以下失败关闭合同：

- runner 默认 disabled；即使两个 runner env 均开启也返回 scheduler 未配置；
- observe enable 返回 `runner_scheduler_not_configured`；
- live pause 返回 `live_pause_disabled`；
- live copy 返回 `copy_persistence_not_configured`，adapter copy 0 次；
- TikTok 返回 `channel_not_enabled`；
- budget/Meta status 等 roadmap 字段返回 `field_not_supported`；
- 没有 copied `created_data/lineage/intent` DDL/DML。

生产仍需验证 feature flags/timer 均未配置，以及手动 observe 的 Token/Graph/Meta 写为 0。

## 9. 遗留风险

- 生产 query plan、候选规模、超时和只读库压力未知。
- 63353 写后到 63350 可见延迟尚未测量。
- optimizer 真实身份与 15 产品 seed 尚未在线验证。
- 快照/MySQL 跨存储非原子且无清理器。
- 计划、额度和时区本地调度仅配置，不执行。
- 三层 live pause、任何 live copy、TT 和 created_data 关联没有发布/Canary 结论。
- 200% 缩放、全键盘、屏幕阅读器、对比度和 Edge 仍待人工验收。

## 10. 发布建议

当前建议：

- 可以进入最终代码评审、commit/push 和受控 staging；
- 在精确 target commit 重跑 132 条后，才可执行八表 DDL/seed；
- 完成 `deploy.md` 的生产 P0 门禁后，才可 route dark 和单范围手动 observe；
- 在生产 V2 回归、三层零外部写证据和线上浏览器完成前，不得标记发布完成；
- 本期任何情况下都不得启用 scheduler、规则 enable 或 Meta 写能力。

本报告需在主发布流程取得生产证据后再次更新最终结论。
