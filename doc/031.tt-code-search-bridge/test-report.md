# 测试报告

## 当前结论

代码、最终全量 TT 回归、独立复审、生产候选迁移、Redis/Nginx/systemd 部署和线上验收均已完成；当前没有未关闭的 P0/P1/P2。生产运行 commit 为 `b01dabe22d9da1571c68b6fb0775a61bb48e18de`。

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

- `python -m unittest discover -s scripts -p "test_tt*.py"`：395 tests，failures/errors=0。
- 新页面 Node bridge：84 assertions；原 `/tt` bridge：53 assertions；均退出 0。
- `compileall`、`py_compile app.py`、`node --check`、`git diff --check` 均退出 0。
- 原 `/tt` HTML/JS/Nginx 三个受保护文件相对基线零 diff。
- 独立终审额外复跑 248 个核心/服务/app/route 测试，结论为无未关闭 P0/P1/P2。

当前组合接口版本已在本地与生产 Chrome 实测。生产 390x844 和 1440x900 均为 Featured 恰好五条、左右箭头可用、鼠标拖动可用；输入修改后旧链接/href 立即清空，过期响应不会覆盖当前输入；全程无 console error 和 page error。

服务器最终全量回归在高 I/O 条件下有一个与本需求无关的 GPU timeout 时序用例偶发失败；同一用例立即单独复跑于 0.169 秒通过。TT code 定向 17 项、新旧 Node 84/53 assertions 均通过；本地 exact diff 的 395 项全量回归为零失败。

## 计划用例状态

`test-cases.md` 当前共 91 条：A 5、B 11、C 16、D 12、E 11、F 16、G 11、H 9。

| 分层 | 当前状态 | 最终证据要求 |
| --- | --- | --- |
| Python 单元/合同 | 通过：本地 exact diff 395 tests，failures/errors=0 | 生产 TT code 定向 17 项通过；单个无关 GPU 时序用例偶发后单测通过 |
| Node bridge | 通过：新 84、旧 53 assertions | exact commit 候选复跑通过 |
| 编译/静态检查 | 通过 | `compileall`、`py_compile`、`node --check`、diff check 均退出 0 |
| 浏览器 | 通过 | 生产 390x844、1440x900；五条、按钮/drag、输入失效/race、无 console/page error |
| DB 副本迁移 | 通过 | 旧计数不变、重复迁移幂等、`integrity_check=ok` |
| Redis 6381 | 通过 | loopback、PONG、缓存活动、停止后 SQLite fallback、恢复 |
| Nginx/服务 | 通过 | `nginx -t`、health、exact route、受影响 unit active |
| 原 `/tt` | 通过 | 三个受保护文件部署前后 SHA-256 完全一致，HTTP 正常 |
| 零真实发布 | 通过 | queue/run/plan/publish ID 基线部署前后不变 |

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

本轮最终结果已于 2026-08-04 补录；运行 Git SHA 与生产 release 均为 `b01dabe22d9da1571c68b6fb0775a61bb48e18de`。

## 缺陷情况

独立评审确认并修复了历史 pending queue、AIpost 默认值、公共 sidecar 暴露、Redis 共享锁、高占用 SQL、URL 顺序、Redis data dir、迁移原子性、回收审计、`{code}` exact retry 误冲突和输入变化后的旧 CTA/race 问题；详见 `bugs.md` 与 `sa-code-review.md`。最终全量与独立终审均通过，当前无已知未关闭 P0/P1/P2。

## 线上证据

- GitHub/runtime SHA：`b01dabe22d9da1571c68b6fb0775a61bb48e18de`；旧 release `af95ea73...`，备份目录及 manifest 已校验。
- 数据盘 UUID、online backup、迁移副本、重复迁移和生产 `integrity_check=ok` 均通过；route/audit 上线后仍为 0。
- Redis 仅监听 `127.0.0.1:6381`，PING=PONG；公共查询产生缓存活动，停止后 SQLite fallback 成功，随后恢复。
- `nginx -t`、主 app、sidecar、Redis 健康；新页面和 JS 为 200/no-store。
- 有效 content ID `ZZ4b4w5k3h` 的 Search/Featured 通用回退分别得到对应 channel；未知 code、不存在 content 均 404。
- 原 `/tt` HTML/JS/Nginx hash 部署前后完全一致。
- queue `7`、max queue `7`、publish IDs `6`、schedule runs `7`、random plans `7` 前后不变，证明验收没有触发真实发布。

## 遗留风险

- 满池回收是产品确认的破坏性语义，虽需多年才可能发生，仍必须持续监控 `tt_post_code_recycle_audit`。
- Redis 与 SQLite 无跨系统事务，namespace 旋转与 SQLite fallback 必须保留，不能用 TTL 代替一致性控制。
- Pointer/touch 合同已有自动化事件链与生产移动视口验证；后续浏览器版本变化仍应纳入常规巡检。
- 生产已有 Redis、新 Nginx、新静态和 DB schema 证据；当前 route/audit 为 0，因此没有通过伪造发布记录验证 code exact/published clone，相关语义由自动化覆盖。

## 发布门禁

本地与生产上线门禁均已通过：DB 副本迁移、Redis/Nginx/systemd、原 `/tt` 线上 hash、回滚材料/回滚点和零真实发布基线均有证据。为避免额外生产风险，未执行 H08 生产代码切回，也未执行 H09 的 unit/config 恢复；只实际验证了 Redis 停机降级与恢复。后续变更仍须重复执行同一门禁。
