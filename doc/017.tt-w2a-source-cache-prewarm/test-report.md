# 测试报告

## 测试结论

**最新加固后的本地自动化、兼容性与静态门禁通过，生产验收待执行。** Python 套件结果为 `104 tests OK (skipped=1)`，唯一跳过项是 Windows 无法验证的 POSIX mode preservation；该项必须在生产 Linux 发布前执行通过。最终全量审计无剩余 P0/P1/P2；当前尚未提交、部署或上线。

## 测试范围

- W2A 原始 HTML GET、白名单字段提取、描述元素边界和精确 `content_id` 校验。
- SQLite 正/负/stale 缓存、复合主键、持久化租约、并发，以及首次记录 parent `st_dev` 和每次 connect 前后设备身份复查。
- resolver/featured API 兼容、共享服务选择、公开字段白名单及内部/公开缓存状态映射。
- 最近三日预热、固定 insight 表/索引、保持花费排名的 cursor v2、bounded retry backlog、fresh 跳过、500/3000 硬上限、限流、bootstrap 与 `--dry-run`。
- featured 花费排名、无剧集元数据查询、last-known-good 和 systemd 配置。
- 生产数据盘、timer、公开 API、`/tt` 浏览器流程、性能、部署和回滚。

## 执行统计

| 类型 | 总计 | 结果/已通过 | 失败 | 跳过/待执行 |
| --- | ---: | ---: | ---: | ---: |
| TT Python 自动化测试 | 104 | OK | 0 | 1（Windows POSIX mode preservation；生产 Linux 必跑） |
| TT Node 断言 | 53 | 53 | 0 | 0 |
| Python 编译/兼容门禁 | 2 | `compileall`、Python 3.9 AST 均 OK | 0 | 0 |
| 补丁格式门禁 | 1 | diff-check OK | 0 | 0 |
| 本地真实源 canary | 2 | 2 | 0 | 0 |
| 需求验收用例 | 38 | 以 `test-cases.md` 逐项状态为准 | 0 | 含生产与浏览器待验项 |
| 生产 systemd/数据盘/API | 待生产执行 | 0 | 0 | 待执行 |
| 真实浏览器回归 | 待生产执行 | 0 | 0 | 待执行 |

## 已验证结果

- TT Python：`104 tests OK (skipped=1)`。
- 唯一 skip 为 Windows 平台上的 POSIX mode preservation；这是平台限制，不计为通过，生产 Linux 必须补跑并通过。
- TT Node：`53 assertions OK`。
- Python `compileall`：OK。
- Python 3.9 AST：OK。
- diff-check：OK。
- 有效 ID canary：`Ag0rfr5F0F` 解析为 `Her Beast`，419.7 ms。
- 错误 ID canary：请求 `ZZZ…` 时源页实际解析为 `Yqq…`，精确 ID 不匹配并被拒绝，332.0 ms。
- 已验证的关键边界包括：无浏览器渲染、无源请求重试、无进程内数据缓存、不持久化源 URL/原始 HTML、描述元素必需但文本可空、复合主键、公开缓存状态兼容映射、cursor v2/有界重试、fixed insight 表/索引、API 无全局 UMask、state `2770`、SQLite DB/WAL/SHM `0660`、首次记录 parent `st_dev`、connect 前后设备/父目录/软链接漂移 fail closed、fresh 跳过及 featured 不查询剧集元数据。

## 缺陷情况

- 最终本地代码评审和自动化审计没有未关闭的 P0/P1/P2。
- 本轮未建立缺陷单；生产未测项不能据此判定为通过。

## 生产待验项

- 数据盘 UUID、挂载点、目录权限、SQLite WAL、租约及服务重启后的缓存持久化。
- 在生产 Linux 执行被 Windows 跳过的 POSIX mode preservation 测试，确认 DB、WAL、SHM 的 `0660` 权限可持续保持。
- 在 Linux 验证首次 mount/UUID 后记录的 parent `st_dev`，并验证 connect 前后发生目录、设备或软链接漂移时关闭连接且不写根盘。
- systemd unit/timer 安装、`RandomizedDelaySec=5m`、journal、失败恢复和 last-known-good。
- 公开 resolver/featured API 的字段、状态、参数透传及错误 ID 行为。
- `/tt` 真实浏览器搜索、封面/简介展示和点击跳转。
- 冷/热缓存生产性能与并发下的 p95。
- GitHub commit/push、release、备份、current 切换与回滚演练。

## 遗留风险

- W2A HTML 结构变化或源站短时不可用。
- API 与 timer 跨进程访问 SQLite 的生产竞争特征尚未实测。
- 预热候选规模、源站压力和旋转游标在真实数据量下尚未验证。
- 旧 MySQL fallback 与 W2A 缓存状态在生产流量中的兼容性尚未验证。

## 发布建议

本地自动化、兼容性与静态门禁已通过，可以进入 GitHub-first 部署流程。当前不得标记为“已发布”或“生产验收通过”；部署期间必须先在 Linux 补跑 POSIX mode preservation 和设备身份保护，再通过 systemd/数据盘、HTTP canary、真实浏览器、性能和回滚门禁。
