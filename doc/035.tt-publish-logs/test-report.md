# 测试报告

## 测试结论

通过并已完成生产部署；本次只回填既有发布记录的 code 路由，未触发新的 TikTok 发布。

## 测试范围

统一日志服务、双账本只读查询、来源/触发/状态/日期筛选、全局分页、安全脱敏、主 API 代理、导航、新页面、旧发布池瘦身，以及 TT Post/TT 自动发布全回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 统一日志专项自动化 | 12 | 12 | 0 | 0 |
| TT 相关完整回归（含专项） | 557 | 557 | 0 | 0 |
| 编译/语法/差异检查 | 4 | 4 | 0 | 0 |
| 浏览器场景验收 | 4 | 4 | 0 | 0 |

## 缺陷情况

开发期发现并关闭 1 个统一状态兼容缺陷，见 `bugs/BUG-001.md`；代码终审另修正时间格式排序和重复刷新监听，无遗留阻塞缺陷。

## 验证证据

- `python -m unittest ...`：`Ran 339 tests ... OK`。
- 页面实际显示两类来源、四种触发方式、统计、筛选、详情和来源查询参数。
- 自动任务详情成功读取原运行事件；390×844 页面可访问，表格保留横向滚动；浏览器控制台 0 error / 0 warning。
- 旧发布池静态契约确认无日志 DOM、无 `/tasks` 请求，素材池与立即测试业务控件仍在。

## 遗留风险

- 两账本默认聚合采用失败关闭；任一账本不可读时“全部来源”页面会明确失败，需修复账本后恢复。
- 移动端沿用后台全局导航布局，长导航会出现在内容之前；未在本需求中调整公共导航。
- 未在生产数据规模上做压测，因此发布日志 offset 限制为 10,000，单页最多 200。

## 发布建议

按 `deploy.md` 通过 GitHub 不可变 commit 发布；先备份 TT auto release 指针、静态文件和 SQLite，只切 TT auto sidecar 与静态资源，不切主 API；只做只读页面验证，不触发真实发布。

## 2026-08-07 4 位码增量

- 专项 32 项通过：页面 code 列、自动高位路由补读、空/非法 code、历史回填 discovery/apply、备份、计划变化、归因渠道、容量、ledger-only 重建和 direct-test 隔离。
- ledger-only 回填单文件 18 项通过：逐 ID opt-in、唯一 recurring/event、账本身份、冻结账号快照、fallback provenance、hash drift、混合事务和失败零写入。
- TT Post / TT 自动发布完整回归 557 项通过。
- Python 编译、JavaScript 语法和 `git diff --check` 通过。
- 所有测试均为离线账本/静态页面验证，没有触发真实 TikTok 发布。
- 生产 SQLite 只读精确核对候选为 q2–q7，且 q2–q4 ledger lineage、q5–q7 frozen URL 均满足门禁。不能用 caption 缺少 4 位 token 代替 `code=''` 的数据库证据。

## 2026-08-07 生产部署与回填证据

- GitHub 不可变发布 commit：`0392013f68825530ac52132c7be3c258650be1de`；`/opt/tt-auto-post/current` 已切到对应 release。主 API、Nginx 配置和 `/opt/tt-post/current` 均未切换。
- 代码/静态文件回滚包：`/mnt/data-disk/tt-publish-log-deploy/backups/20260807T074452Z-0392013f68825530ac52132c7be3c258650be1de`。
- 回填前 SQLite 在线备份：`/mnt/data-disk/tt-post-publisher/backups/20260807T074739Z-code-backfill-0392013/tt-post-before-code-backfill.sqlite3`，SHA-256 为 `c8471fa1ad51e4fe5ba7ca359b1fa677acd1da3fd67d1f8d1981379d36494135`；主库和备份均 `quick_check=ok`。
- exact dry-run 计划为 6 条，SHA-256 为 `05ac3b62af41c8e09b2d7b3f6efbaac4b3e3fa674f0f8cffb04cc22f8da65b49`；apply 后 route 总数 `86 -> 92`、非空 queue code `82 -> 88`，queue/publish_id 数量、caption 与除 code 外的 q2–q7 不变量均未改变，候选归零。
- 最终映射：q2=`5JD1`、q3=`LSDW`、q4=`WT3N`、q5=`BWBV`、q6=`ITW0`、q7=`7OOB`；六个公开 resolver 请求均为 `found=true`、`route_mode=code_exact`，并命中各自冻结的 content ID。
- 已登录浏览器验收 `/tt-publish-logs.html`：表头存在 `4位码`，第 5 页 q2–q7 显示上述映射，`direct_test1` 仍显示 `—`，控制台 0 error / 0 warning；未执行真实发帖。
- `tt-auto-post-service`、`tt-post-service` 及相关 scheduler/runner/path/prepare/metric 单元最终均为 active，两侧 health 均 `ok=true`。
