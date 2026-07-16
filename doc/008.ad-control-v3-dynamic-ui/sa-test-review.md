# SA 测试用例评审

## 结论

通过本地测试设计和生产 R1 验收评审；允许“FB 配置 + 手动 observe”生产使用，未发布能力仍禁止。

`test-cases.md` 已按冻结实现重新标注 88 条用例，清楚区分：本地自动化、本地 mock Playwright、真实 MySQL、生产浏览器、V2 回归和未发布能力。本地/服务器 139/139 与生产 Canary 分开记录，未将单测扩张为 Meta 放量证据。

## 评审范围

- 两个动态页面与权限/路由；
- 产品多选、optimizer 身份、账户字段禁入和可选时区；
- Campaign/Ad Set/Ad 三层字段目录、规则引擎和 Copy 配置；
- 手动 observe、Preview/Execution 事务、日志与数据盘；
- scheduler/enable/live/copy/TT/created_data/清理器门禁；
- exact-source 部署和 V2 零影响；
- 响应式、XSS、UTF-8 与可访问性。

## 评审问题与闭环

| 编号 | 级别 | 问题 | 修订 | 状态 |
| --- | --- | --- | --- | --- |
| STR-001 | P0 | 原用例把 runner observe 当作本期能力 | TC-054～064 明确计划仅配置、只有 manual preview、enable/runner 失败关闭 | 已修订 |
| STR-002 | P0 | 原用例将 live pause/copy 当作待 Canary 的当前代码 | 改为门禁与零 external mutator；真实 Meta Canary 移出本期 | 已修订 |
| STR-003 | P0 | `content_id` 源列不存在 | TC-049 固定指定剧使用 `series_code`，content_id 不可筛 | 已修订 |
| STR-004 | P0 | 预算/Meta 状态字段被写成当前可筛 | TC-037 明确 roadmap 字段 UI/服务端双禁 | 已修订 |
| STR-005 | P0 | 单测可能被误写为生产通过 | 真实 MySQL、生产权限、V2 和浏览器均单独取证；V2 自然 tick 仅观察、不人为触发 | 已修订 |
| STR-006 | P0 | 快照清理用例假定已有实现 | TC-073 改为阻塞/未发布，禁止批删 | 已修订 |
| STR-007 | P1 | 原日志用例要求业务日聚合，超出冻结实现 | TC-074 改为 V3 event list，不承诺业务日合并 | 已修订 |
| STR-008 | P1 | 原浏览器验收范围大于已有证据 | 仅 1440/390、无溢出、中文、console 标通过；200%/键盘/对比度继续待验 | 已修订 |
| STR-009 | P1 | 后端通用 idempotency key 未实现 | TC-067 收窄为 UI mutation single-flight，不宣称后端幂等键 | 已修订 |

## 自动化映射

- `test_ad_control_v3_core.py`：身份、schema、三层 adapter、bounded query、时区/歧义、规则引擎、门禁、数据盘。
- `test_ad_control_v3_repository.py`：八表 allowlist、读写分离、事务、target 上限、CAS、DDL/seed/rollback 契约。
- `test_ad_control_v3_routes.py`：动态路由、认证/module、same-origin JSON、method/body 上限、lazy app wiring。
- `test_ad_control_v3_ui.py`：两动态页、无默认值、三层/Copy UI、server pagination、XSS、responsive CSS、navigation contract。
- `test_ad_control_v3_deploy.py`：精确 source/target、drift、锁、backup、幂等 apply、自动 rollback 和显式 release rollback。

总计 139/139；其中 core/查询性能专项 59/59、navigation 发布链 13/13。本地两次最终运行约 81.786 秒和 79.729 秒，服务器精确 commit 运行约 4.058 秒；生产数据库和线上浏览器另有独立证据。

## Playwright 评审

冻结代码 mock 证据已覆盖：

- 规则页/日志页，1440 与 390；
- UTF-8 中文完整；
- 无页面级横向溢出；
- 页面明确调度器未发布、仅保存草稿+手动试算、启用锁定；
- execution 展示 manual preview / observed；
- console 0 Errors / 0 Warnings。

生产 admin 登录态、真实规则/日志数据、三层试算和对象详情已覆盖。普通用户/无模块权限身份、权限回收、200% 缩放、全键盘、屏幕阅读器、对比度和 Edge 仍保留待执行。

## 生产 QA 门禁执行结果

1. 精确 commit staging 139/139 和 Python 3.9/JS/diff 检查通过。
2. 八表 DDL、15 产品 seed、reader/writer 和 read-after-write 回读通过。
3. admin 真实身份与未登录 401 通过；普通用户/无模块账号保留后续补测。
4. Campaign/Ad Set/Ad 分别手动 observe，三次 Meta 写均为 0。
5. 数据盘 runtime、权限、hash 和快照回读通过。
6. V2 文件、API、SQLite integrity、cron、runner hash与导航发布前后对比通过；发布后连续 8 个自然 tick 为零动作 `no_accounts_due`。
7. 线上 Chrome 动态两页、真实数据、编辑/试算/日志/详情通过；人工无障碍专项仍待补。

当前证据只支持手动 observe R1；任何 scheduler、enable、live pause/copy、TT 或 copied created_data 仍不得给出“可放量”。

## 评审记录

- 2026-07-16：初评覆盖 88 条高风险用例。
- 2026-07-16：按冻结实现二次评审，纠正未发布能力与证据状态，结论为“本地测试设计通过、生产待验”。
- 2026-07-16：完成生产 R1 QA 取证；结论更新为“手动 observe 通过，自动调控/Meta 写仍未发布”。
