# 测试报告

## 测试结论

**本地、Linux 与生产验收全部通过，最终审计无剩余 P0/P1/P2。** 生产运行版本来自 commit `e77dba9c5d742e5e982c3faa44e9303761f0ff0b`，release 为 `/mnt/data-disk/tt-drama-resource-cache/releases/ai-tt-w2a-cache-e77dba9c5d74`，生产 app 文件 SHA-256 为 `ac68d0cc7c4b58ce9a242b6c12d6b45391b57f847a5e0801aff30d6f69310398`。

## 测试范围

- W2A 原始 HTML GET、白名单字段提取、描述元素边界和精确 `content_id` 校验。
- SQLite 正/负/stale 缓存、复合主键、持久化租约、并发、跨用户权限及 connect 前后设备身份复查。
- resolver/featured API 兼容、共享服务选择、公开字段白名单及内部/公开缓存状态映射。
- 最近三日预热、固定 insight 表/索引、cursor v2、有界重试、fresh 跳过、500/3000 硬上限和 timer。
- featured 昨日花费排名、共享资源缓存、last-known-good 和卡片跳转。
- `/tt` 真实浏览器展示、错误 ID、追踪参数透传和缓存命中性能。
- GitHub-first release、备份、生产文件哈希和回滚点。

## 执行统计

| 类型 | 总计 | 已通过 | 失败 | 跳过 |
| --- | ---: | ---: | ---: | ---: |
| Linux TT Python 测试 | 104 | 104 | 0 | 0 |
| TT Node 断言 | 53 | 53 | 0 | 0 |
| X pool 回归 | 7 | 7 | 0 | 0 |
| X routes 回归 | 14 | 14 | 0 | 0 |
| Python 编译/兼容门禁 | 2 | 2 | 0 | 0 |
| 补丁格式门禁 | 1 | 1 | 0 | 0 |
| 生产缓存 HIT 性能采样 | 30 | 30 | 0 | 0 |
| 需求验收用例 | 38 | 38 | 0 | 0 |

`compileall`、Python 3.9 AST 和 diff-check 均为 OK。此前 Windows 跳过的 POSIX mode preservation 已由 Linux 测试及生产跨用户 SQLite/WAL 权限 canary 覆盖并通过。

## 生产发布证据

- Release commit：`e77dba9c5d742e5e982c3faa44e9303761f0ff0b`
- Release：`/mnt/data-disk/tt-drama-resource-cache/releases/ai-tt-w2a-cache-e77dba9c5d74`
- 备份：`20260727T092255Z-predeploy`
- 并发 X 基线备份：`20260727T094144Z-concurrent-x-baseline`
- 生产 app SHA-256：`ac68d0cc7c4b58ce9a242b6c12d6b45391b57f847a5e0801aff30d6f69310398`

## 生产功能证据

- 有效 ID `Ag0rfr5F0F` 返回 `Her Beast` 及 CDN 封面。
- 错误 ID `ZZZZZZZZZZ` 首次返回 `404` 与 `MISS`，再次返回 `404` 与 `NEGATIVE_HIT`。
- 短 ID 返回 `400` 与 `BYPASS`。
- 连续 30 次 `HIT`：p50 `13.358 ms`、p95 `14.162 ms`、最大值 `15.401 ms`，满足 p95 小于 50 ms 的验收目标。
- Featured 的 `source_date` 为 `2026-07-26`，共 5 项，卡片均可跳转。
- 预热候选 2880，处理 500，填充 495，已有缓存 5，错误 0；cursor `next_index=500`。
- systemd 定时任务正常；验收快照中 prewarm 下一次触发时间为 `2026-07-27 20:24:53 CST`。
- API 与离线任务跨用户访问 SQLite/WAL 的权限 canary 通过。
- 真实浏览器显示封面、标题和简介；错误 ID 不显示卡片；`af_adset_id=XXX` 正确透传。错误 ID 在 console 中只有预期的接口 `404 Failed to load resource`，没有 JavaScript exception，也没有 CSP warning/error。

## 生产复核状态

- Current release 与 app hash 复核未变化。
- API PID：`2462658`
- `NRestarts=0`
- `TT_DRAMA_RESOURCE_SOURCE=w2a_cache`
- 两个 TT timer 均为 `active/waiting`
- SQLite 共 501 条：`ready=500`、`not_found=1`
- 复核时有效 ID 为 `HIT`、错误 ID 为 `NEGATIVE_HIT`、featured 为 5 项

## 缺陷情况

- 最终代码、自动化和生产验收无剩余 P0/P1/P2。
- 本轮生产证据未发现阻断发布的问题。

## 持续监控项

- 监控 W2A HTML 结构变化、源站错误率及 stale 返回。
- 监控 SQLite/WAL 权限、数据盘设备身份、剩余空间和锁等待。
- 监控预热错误数、cursor 推进、timer journal 和 featured last-known-good。

## 发布结论

生产发布与验收已通过。当前 release、备份、哈希、API、缓存性能、featured、prewarm、timer、权限和真实浏览器证据均已记录，可按 `deploy.md` 中的回滚点执行恢复。
