# 测试报告

## 测试结论

通过，建议在完成生产备份副本迁移验证后发布。所有测试均使用临时 SQLite、mock Sidecar/素材源或本地 HTTP harness，没有真实 X 写入。

## 测试范围

时间解析和到期、幂等、reservation 并发/释放、DB trigger、旧 schema 迁移、auto_template 兼容、API/DTO、manual runner、UI 静态契约及真实浏览器交互、完整 X 发布回归。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 完整 X Python 回归（40 模块隔离执行） | 627 | 625 | 0 | 0（2 项既有条件跳过） |
| Playwright 本地 smoke | 1 | 1 | 0 | 0 |
| Python 编译 / diff 检查 | 2 | 2 | 0 | 0 |

## 缺陷情况

- BUG-001：active reservation 未进入 unavailable material key 查询；已修复并关闭。
- BUG-002：future manual reservation 未排除既有 pool 自动候选；已修复并关闭。
- BUG-003：并发主 API 部署覆盖手动发布时间字段；以 `11db78a…` 为基线合并 TT 强制关闭补丁，生产只读验收通过并关闭。
- 无未关闭 P0/P1/P2 缺陷。

## 验证证据

- scheduled task 在 `scheduled_at-1s` 为 `found=false`，到点后才进入 `running`；第二次 claim 只恢复同一 run。
- `x_post_manual_material_reservation` 在等待时为 active，建队列后 consumed，零队列 preflight failure 后 released。
- operator manual 仍可显式选择创建任务前已在 pool/历史 queue 中的素材；任务创建后，pool 自动候选、另一 manual、auto_template、直接 queue SQL 绕过均无法占用 active material。
- 非规范 DB 时间被 timing trigger 拒绝；迁移重复执行且 `integrity_check=ok`。
- 浏览器截获请求体包含 `publish_mode=scheduled`、分钟精度 `+08:00`；响应显示固定北京时间和“等待定时发布”。
- BUG-003 合并回归：X/TT 主 API 契约 39 项、X 手动发布/runner/UI 相关 40 项通过；生产立即/定时完整字段 payload 均在无 Cookie 边界返回 401，而非字段校验 400，账本保持 `7/182/182/181/0`。
- 40 个 `scripts/test_x*.py` 模块逐个新进程执行：627 项、625 passed、2 skipped、0 failed。
- 单进程 discover 两次分别在不同的本地 HTTP 用例遇到 Windows `10053`；对应单测、`test_x_accounts` 60 项以及全部模块隔离执行均通过，判定为本机连接层偶发，不是可复现代码缺陷。

## 遗留风险

- 本期无取消/改期；远期任务期间账号或素材变化会在到期预检时 fail closed。
- timer 为 15 秒轮询，正常执行可能晚于设定分钟最多一个轮询周期，且受共享单任务发布锁影响。
- 生产 live 基线 `09d267db…` 在 Premium relay/repost 与素材池时长变更之上包含 operator-manual pool/历史素材复用；本功能已重放到该组合版本，部署和回滚必须整体保留这些现有行为。

## 发布建议

先在生产在线备份副本连续执行两次 schema 迁移，再按 Sidecar → main API → public static 顺序部署；验收只观察 natural `no_pending`/`no_due` 和账本计数，不创建真实任务。
