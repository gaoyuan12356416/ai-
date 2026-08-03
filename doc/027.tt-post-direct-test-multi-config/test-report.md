# 测试报告

## 当前结论

**027 基线与 BUG-005 增量均已部署；本地、服务器候选和生产只读验收全部通过。**

截至 2026-08-03，BUG-005 增量后的 9 个 Python 测试脚本在本地和服务器候选版本均为 `341/341`，Node bridge 均为 `53/53`；目标 Python 文件编译和 `git diff --check` 通过。专项覆盖同 ID 隔离、完整状态桶、并列时间稳定分页、已发布 direct-test、旧 `/queue` 快照、查询 0 DB/GPU 写入、UI direct 只读操作和代理白名单。一个慢响应覆盖新筛选的竞态已修复并复验。

## 计划范围

- `/test-publish` 独立立即测试、非自动成员账号、历史 published 素材、新 GPU job、同素材 active/unknown 临时互斥和 pool 隔离；direct 明确终态后不阻断正常 auto claim；
- 素材 `published|unknown|unpublished` 三态扁平字段与计数；
- 描述、开关/时间、多账号的单版本原子配置，关闭不要求新 consent；
- 同分钟全部 slots 在首个 creator-info 前调用现有 `claim_recurring_run`，逐项原子、无 due 表；
- 两张 additive 表、legacy mixed 两步迁移、回滚不覆盖 SQLite/ledger；
- 权限、脱敏、UI 路由、GPU/COS/短链回归和生产零写入。
- BUG-005：通过只读 `/tasks` 在主表统一展示自动/排期与立即测试，覆盖任务类型、统计、筛选、稳定分页、跨表 ID/操作隔离和旧 `/queue` 回归。

## 执行统计

| 类型 | 计划 | 已执行 | 通过 | 失败 | 待执行 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 独立 direct-test（D） | 23 | 23 | 23 | 0 | 0 |
| 统一发布任务主表（T） | 12 | 12 | 12 | 0 | 0 |
| 发布状态/auto-direct 互斥（P） | 12 | 12 | 12 | 0 | 0 |
| 原子配置/UI（C） | 22 | 22 | 22 | 0 | 0 |
| 同分钟 preclaim（S） | 10 | 10 | 10 | 0 | 0 |
| 迁移/回滚（M） | 10 | 10 | 10 | 0 | 0 |
| 安全/无副作用（N） | 10 | 10 | 10 | 0 | 0 |
| **合计** | **99** | **99** | **99** | **0** | **0** |

## BUG-005 后全量自动化证据

| 脚本 | 结果 |
| --- | ---: |
| `test_tt_post_direct_config_core.py` | 8/8 |
| `test_tt_post_links.py` | 6/6 |
| `test_tt_post_pool_ui.py` | 33/33 |
| `test_tt_post_prepare_runner.py` | 16/16 |
| `test_tt_posts_app_contract.py` | 13/13 |
| `test_tt_posts_core.py` | 68/68 |
| `test_tt_posts_service.py` | 119/119 |
| `test_tt_gpu_worker.py` | 67/67 |
| `test_tt_account_settings_ui.py` | 11/11 |
| **Python 合计** | **341/341** |

`test_tt_drama_bridge.js` 为 `53/53`；内联页面 JavaScript 语法、6 个目标 Python 文件编译和 `git diff --check` 均通过。

BUG-005 新增 service 2 项、UI 3 项、app contract 1 项，分别覆盖 T01-T12 的服务、页面和代理合同；测试全部使用临时 DB、fake GPU/TT，不连接真实发布接口。

## 027 基线生产验收证据

1. 生产代码 commit 为 `9fd0f99843d45269a5f2e4f0c7028c56321e427c`，不可变 release 为 `/opt/tt-post/releases/9fd0f99843d45269a5f2e4f0c7028c56321e427c`；远端分支引用一致。
2. SQLite online backup 位于 `/mnt/data-disk/tt-post-publisher/backups/20260803T085637Z-282eb91-to-9fd0f99-direct-multi`；DB-COPY 连续初始化两次通过，`integrity_check=ok`，旧表统计无变化。
3. sidecar、主 API、runner/prepare timer/path 正常；三份静态页与公网响应 SHA-256 均为 `b0e9ac232a1a4548a201858b7e490a74474d5e449d671df481974abbf2e95de9`。
4. 内部只读 GET 返回 auto-config version 0、enabled=false、账号 640 为 paused；direct-test 为 0 条；素材统计为 published=3、unpublished=2。
5. 验收前后 schedule/pool/queue/run/intake 状态与数量一致；没有 POST 配置、立即测试或素材池接口，没有真实发布。
6. 浏览器已刷新公网静态页并确认新文案加载；Chrome 与应用内浏览器均无登录态，因此未代为登录，也未触碰保存/发布控件。已登录态下的 UI 行为由 30/30 页面断言和服务端只读 GET 共同覆盖。

## BUG-005 本地与生产证据

- 已修复：主发布任务表、total/summary、筛选和分页统一包含 `tt_post_queue` 与 `tt_post_direct_test`；详情见 `bugs/BUG-005.md`。
- 已实现合同：新增纯 GET `/tasks`，服务端在单一读快照中合并后筛选、稳定排序、分页前统计再分页；旧 `/queue` 完全保持。
- 操作边界：direct 行只读，不显示或调用 queue events/cancel/reconcile；相同数字 ID 由 `task_key/task_type` 隔离。
- 已完成：T01-T12、9 个 Python 脚本全量回归、Node bridge、临时 DB `/queue` 前后快照和查询前后 DB hash 0 diff。
- 生产已完成：GET `/tasks` 命中 2026-08-03 的 `direct_test:1`，total=5、published=4；旧 `/queue` total=4。
- 生产 0 副作用：10 张 TT 表的部署前后逻辑指纹相同，`integrity_check=ok`，已有 publish ID 未变；未调用任何写接口。
- 登录态 Chrome 已刷新，主表显示“共 5 条 / 已发布 4 条”，并显示“立即测试 1 / 素材 5837129 / 已发布 / 查看详情”。
- 禁令：不得通过创建真实 direct-test、修改自动配置或触发发布来验证列表；生产只使用已有事实执行 GET。

## 已关闭的阻断风险

- direct target 已与 auto membership 解耦；只有自动入池要求已保存成员。
- pending request 冻结 key/config version/consent time，非终态不清除；确定 4xx 不保留幽灵 key。
- direct/auto 同素材与同账号远端发布事务互斥，过期 publishing 全量隔离为 unknown。
- 当前全部 due slot 在任何 creator-info 前原子预占；自动发布优先，存在自动任务的 tick 不执行 direct。
- direct 明细每 10 秒只读刷新非终态，unknown 停止自动轮询并保留人工核对提示。

非阻塞 P2：素材池聚合当前每类最多读取 1000 条；长期超过上限后应改为数据库分页/分块聚合，不影响本次上线。

## 发布结论

**BUG-005 已通过生产只读验收并关闭。** 验收只读取已有任务，没有创建测试 Post、保存配置或消费素材池。
