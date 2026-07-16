# SA 测试用例评审

## 结论

通过本地测试设计评审；允许作为 GitHub 提交门禁。生产发布结论仍为待验。

`test-cases.md` 已按冻结实现重新标注 88 条用例，清楚区分：本地自动化、本地 mock Playwright、真实 MySQL、生产浏览器、V2 回归和未发布能力。未将 132/132 本地结果扩张为生产证据。

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
| STR-005 | P0 | 132 条单测可能被误写为生产通过 | 所有真实 MySQL、生产权限、V2、自然 tick 单独标待执行 | 已修订 |
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

总计 132/132；其中 core/查询性能专项 52/52、product/安全相关子集 56/56、navigation 发布链 13/13。该数字来自主流程实际执行，不包含生产数据库或线上浏览器。

## Playwright 评审

冻结代码 mock 证据已覆盖：

- 规则页/日志页，1440 与 390；
- UTF-8 中文完整；
- 无页面级横向溢出；
- 页面明确调度器未发布、仅保存草稿+手动试算、启用锁定；
- execution 展示 manual preview / observed；
- console 0 Errors / 0 Warnings。

未覆盖生产登录态、权限回收、真实分页、200% 缩放、全键盘、屏幕阅读器、对比度和 Edge，均保留待执行。

## 生产 QA 门禁

1. 精确 commit staging 重跑 132 条测试和 Python 3.9/JS 检查。
2. 八表 DDL、15 产品 seed、reader/writer 和 replication readback。
3. admin/普通用户/无权限三种真实身份。
4. Campaign/Ad Set/Ad 分别手动 observe，Token/Graph/Meta 写 0。
5. 数据盘 mount、权限、hash 和系统盘零运行文件。
6. V2 文件、API、SQLite、cron、runner 与自然 tick 发布前后对比。
7. 线上 Chrome 1440/390 和关键人工无障碍回归。

任一 P0 未取得证据，测试报告不得给出“可放量”。

## 评审记录

- 2026-07-16：初评覆盖 88 条高风险用例。
- 2026-07-16：按冻结实现二次评审，纠正未发布能力与证据状态，结论为“本地测试设计通过、生产待验”。
