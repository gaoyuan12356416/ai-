# 测试报告

## 当前结论

代码已实现，最终 diff 的全量 TT 回归、独立复审和本地真实浏览器验证均已通过；当前没有未关闭的 P0/P1/P2，可进入受控生产部署。生产候选迁移、Redis/Nginx/systemd 和线上验收仍须按本文件门禁执行。

本次验证严禁真实 TikTok publish、canary、`run-now`、scheduler 人工触发或 schedule-save。

## 当前覆盖

已实现的自动化覆盖：

- route/audit schema、加法迁移回滚、code 格式、随机碰撞、位图空槽、满池回收/审计和幂等。
- 所有正式 queue 的 code/route/caption 原子冻结，`{code}` preview、非法变体、UTF-16、直接测试拒绝、历史 queue 兼容，以及相同 payload/idempotency_key 的 exact replay。
- 新正式 TT URL、旧 AIpost 兼容、`af_dp` 第一、clone/fallback 和 target allowlist。
- Redis hit/miss/负缓存/坏缓存/失效/namespace/停止/超时；两阶段失效在事务共享锁内 rotate namespace、锁外 Redis DEL，慢 Redis 读/删不占 queue 写锁。
- sidecar loopback bearer、主 app 公共输入/限流/gate/剧目校验/组合响应和安全错误。
- 新页面五条 Featured、code 大写、一次组合 API、按钮/drag/snap/不误触、输入变化立即清空旧 href/结果、pending abort 和过期响应隔离；原 bridge Node 合同回归。
- 原 `/tt` 三个受保护源文件零 diff 检查。

## 阶段性证据

2026-08-04 当前最终 diff 的本地证据：

- `python -m unittest discover -s scripts -p "test_tt*.py"`：394 tests，failures/errors=0。
- 新页面 Node bridge：84 assertions；原 `/tt` bridge：53 assertions；均退出 0。
- `compileall`、`py_compile app.py`、`node --check`、`git diff --check` 均退出 0。
- 原 `/tt` HTML/JS/Nginx 三个受保护文件相对基线零 diff。
- 独立终审额外复跑 248 个核心/服务/app/route 测试，结论为无未关闭 P0/P1/P2。

当前组合接口版本已在本地 Chrome 390x844 实测：Featured 恰好五条、左右箭头可用、鼠标拖动可用；输入修改后旧链接/href 立即清空，已发出的过期响应不会覆盖当前输入；全程无 console error 和 page error。桌面视口和生产资源仍需在最终 commit/上线后复验。

## 计划用例状态

`test-cases.md` 当前共 91 条：A 5、B 11、C 16、D 12、E 11、F 16、G 11、H 9。

| 分层 | 当前状态 | 最终证据要求 |
| --- | --- | --- |
| Python 单元/合同 | 最终全量通过：394 tests，failures/errors=0 | 生产候选按 exact commit 重跑 |
| Node bridge | 最终通过：新 84、旧 53 assertions | 生产候选按 exact commit 重跑 |
| 编译/静态检查 | 全部退出 0 | 候选环境继续执行配置/服务验证 |
| 浏览器 | 当前组合合同 Chrome 390x844 已通过；桌面/生产待验收 | 1440x900、线上一次 API、五条、drag 不误触、输入失效/race、无 console/page error |
| DB 副本迁移 | 待候选环境 | 旧库计数不变、新 schema 正确、`integrity_check=ok` |
| Redis 6381 | fake 已覆盖，生产待部署 | 仅 loopback、PONG、缓存填充、停止后 SQLite fallback、恢复 |
| Nginx/服务 | 待部署 | `nginx -t`、health、exact route、受影响 unit 正常 |
| 原 `/tt` | 本地源零 diff，线上待验收 | 部署前后 SHA-256、HTTP 和浏览器主流程一致 |
| 零真实发布 | 自动化隔离，生产待基线 | queue/run/publish ID 计数与发布调用审计 |

## 最终本地命令

```powershell
python -m unittest discover -s scripts -p "test_tt*.py"
python -m compileall -q features/tt_posts scripts/tt_post_service.py scripts/tt_post_runner.py scripts/tt_post_prepare_runner.py
python -m py_compile app.py
node --check static/tt-drama-code-search.js
node scripts/test_tt_drama_code_bridge.js
node scripts/test_tt_drama_bridge.js
git diff --check
git diff --exit-code -- static/tt-drama-search.html static/tt-drama-search.js deploy/nginx/tt-drama-search.conf
```

本轮最终结果已于 2026-08-04 补录；exact Git SHA 在提交后与生产 release 一并补录。

## 缺陷情况

独立评审确认并修复了历史 pending queue、AIpost 默认值、公共 sidecar 暴露、Redis 共享锁、高占用 SQL、URL 顺序、Redis data dir、迁移原子性、回收审计、`{code}` exact retry 误冲突和输入变化后的旧 CTA/race 问题；详见 `bugs.md` 与 `sa-code-review.md`。最终全量与独立终审均通过，当前无已知未关闭 P0/P1/P2。

## 上线后待补录

- GitHub exact SHA、新/旧 release、backup 目录和 manifest。
- 数据盘 mount/UUID/空间，online backup 与迁移副本结果。
- Redis 安装版本、unit 验证、监听地址、DBSIZE/PING、停止/恢复 fallback。
- Nginx 配置、主 app/sidecar PID 和 health。
- 公共 code miss、有效 content ID fallback、Featured fallback/clone（仅有现成 published 数据时）的实际响应摘要。
- `/tt-code` 页面/JS hash、移动和桌面浏览器证据。
- 原 `/tt` 部署前后 hash 与行为。
- queue、max queue ID、publish ID、run 等前后基线，证明没有为验收触发真实发布。

## 遗留风险

- 满池回收是产品确认的破坏性语义，虽需多年才可能发生，仍必须持续监控 `tt_post_code_recycle_audit`。
- Redis 与 SQLite 无跨系统事务，namespace 旋转与 SQLite fallback 必须保留，不能用 TTL 代替一致性控制。
- Pointer/touch 行为需以最终线上资源和真实浏览器事件链复验。
- 生产目前尚无本需求 Redis/新 Nginx/新静态/DB schema 证据。

## 发布门禁

本地门禁（最终全量、独立复审、浏览器、原 `/tt` 源文件零变化）已通过。生产切换仍必须满足 DB 副本迁移成功、Redis/Nginx/systemd 候选验证通过、原 `/tt` 线上 hash 零变化、回滚点齐全且零真实发布基线成立。
