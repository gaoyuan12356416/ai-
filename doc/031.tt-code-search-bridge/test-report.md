# 测试报告

## 测试结论

待测试，当前不建议发布。

本目录仅完成需求、架构、API、用例和部署计划。业务代码与实际浏览器/Redis/迁移/生产验证尚未完成，不能声明功能通过。

## 测试范围

计划覆盖：

- `tt_post_code_route` 加法迁移、code 分配、碰撞、高占用、全容量回收和幂等。
- `{code}` preview/冻结/一次渲染/UTF-16 边界。
- 正式 TT URL、published clone、generic fallback 和安全编码。
- `/api/public/tt-code/resolve` 的 code/content ID、Search/Featured、错误和限流。
- Redis 6381 hit/miss/停止/超时/陈旧缓存/恢复。
- `/tt-code` 五条 Featured、触摸/鼠标/按钮/键盘与不误触。
- 原 `/tt`、TT 发布池、短链、排期、GPU 和 X 路由回归。
- GitHub-first 候选、DB 副本迁移、生产只读验收和回滚。

## 执行统计

| 类型 | 计划用例 | 已执行 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 页面/路由 | 5 | 0 | 0 | 0 | 5 |
| Featured 交互 | 11 | 0 | 0 | 0 | 11 |
| code/schema | 16 | 0 | 0 | 0 | 16 |
| caption 宏 | 10 | 0 | 0 | 0 | 10 |
| URL/归因 | 10 | 0 | 0 | 0 | 10 |
| 公共 resolver | 16 | 0 | 0 | 0 | 16 |
| Redis | 10 | 0 | 0 | 0 | 10 |
| 部署/回滚 | 9 | 0 | 0 | 0 | 9 |
| 合计 | 87 | 0 | 0 | 0 | 87 |

阻塞原因：对应业务代码和候选部署尚未完成。这是正常前置状态，不是测试通过。

## 缺陷情况

当前无已确认缺陷；详见 `bugs.md`。代码评审和测试开始后按实际发现更新。

## 验证证据

待补录：

- 本地 branch/commit、执行时间和依赖版本。
- Python 编译、单元/合同测试、Node/browser 测试、`git diff --check` 的原始结果摘要。
- 临时 SQLite schema、计数、`integrity_check`、并发/回收证据。
- fake/生产 Redis 监听、hit/miss/bypass 和陈旧缓存证据。
- 390x844、桌面、触摸/鼠标/按钮/键盘浏览器截图或 trace。
- 原 `/tt` 部署前后 SHA-256 与浏览器回归。
- GitHub exact SHA、候选 release、backup manifest、服务状态和回滚演练。
- 发布调用/queue/publish ledger 基线，证明没有为验收触发真实 TikTok 发布。

## 遗留风险

- 四位空间全满后会按产品确认回收最早 code，历史 code 将改指向新发布；必须监控并审计。
- Redis 与 SQLite 无跨系统事务，代码必须在缓存失效失败时旋转 namespace 并旁路，不能只依赖 TTL。
- Pointer/touch 行为受浏览器影响，需要真实设备或等价浏览器事件链验证。
- 同期 TT 分支合并可能产生语义覆盖，需以最终 commit 做完整代码评审。

## 发布建议

当前：不发布。

只有 87 条计划用例及全量 TT 回归实际完成、P0/P1 缺陷关闭、SA 代码评审通过、DB/Redis/Nginx/静态备份与回滚点齐全，并且验收未触发真实 TikTok 发布后，才可更新为“建议发布”。
