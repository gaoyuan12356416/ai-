# SA 代码评审

## 结论

多轮独立评审发现的兼容性和边界问题已全部修订；最终 diff 完成 395 项全量回归且无未关闭 P0/P1/P2。生产候选、DB/Redis/Nginx/systemd 及回滚材料/回滚点门禁已验证，运行代码为 `b01dabe22d9da1571c68b6fb0775a61bb48e18de`；未做破坏性生产代码切回。

## 评审范围

- `features/tt_posts/code_routes.py`, `core.py`, `links.py`, `service.py`
- `app.py`
- `static/tt-drama-code-search.html/js`, `static/tt-post-pool.html`
- Nginx、Redis config/unit、env examples
- 新增/修改的 `scripts/test_tt_*`

## 独立评审发现与关闭记录

| 编号 | 级别 | 发现 | 修订 | 状态 |
| --- | --- | --- | --- | --- |
| CR-F01 | P0 | 迁移前已冻结但尚未发布的 `{url}` queue 没有 route/code，升级后可能无法继续 | 无 code 的历史 queue 在生成缺失 long URL 时走原 `AIpost` 兼容路径；不强制补 route | 已关闭，全量回归通过 |
| CR-F02 | P0 | 改动 `build_w2a_url` 默认 channel 会让直接测试和旧调用从 AIpost 漂移到 TT | 默认恢复 `AIpost`；只有新正式 queue 显式传 `channel=TT, af_dp_first=True` | 已关闭，全量回归通过 |
| CR-F03 | P0 | 将 sidecar resolver 直接公开会绕过既有 content 校验、限流和并发门 | Nginx 改为主 app 8787；sidecar 改成 loopback bearer `/internal/tt-posts/code-resolve`；主 app 合并 DramaWave 元数据 | 已关闭，全量回归通过 |
| CR-F04 | P1 | Redis 网络阻塞可能占用 queue/route 的共享写锁 | lookup 在锁外做 GET/SET；两阶段失效在事务共享锁内先旋转 namespace，再在锁外 best-effort DELETE；publish reconcile 释放共享锁后才执行网络失效；增加慢读/慢删并发测试 | 已关闭，全量回归通过 |
| CR-F05 | P1 | 高占用兜底若逐 code 发 SQL，最坏会执行 1,679,616 次查询 | 一次读取占用 code 并构建 bytearray 位图，O(capacity) 内存扫描空槽 | 已关闭，全量回归通过 |
| CR-F06 | P1 | 新正式 URL 与需求给定的 `af_dp` 第一顺序不一致 | 新正式和 clone URL 都使用 `af_dp,c,...,af_c_id`；validator 同时兼容历史 c-first | 已关闭，全量回归通过 |
| CR-F07 | P1 | Redis unit 启动时 data dir 可能不存在；候选机进一步证明主 unit 的 mount namespace 会先于 `ExecStartPre` 建立，首次启动返回 `226/NAMESPACE` | 拆分最小权限 `tt-post` oneshot prepare unit：以 `RequiresMountsFor` 等待数据盘并通过 mount condition 后，于既有 `tt-post` 父目录创建 0700 子目录；主 Redis unit 通过 `Requires/After` 等待，且仍只写子目录 | 已关闭，exact commit 首次启动复验通过 |
| CR-F08 | P1 | `executescript` 会隐式提交，route 表和 queue.code 迁移可能不在同一事务 | baseline script 后显式新开 `BEGIN IMMEDIATE`，其后的加法迁移一起提交/回滚 | 已关闭，DB 副本迁移/回滚/幂等验证通过 |
| CR-F09 | P1 | 满池回收只有结果、缺少持久审计 | 增加 `tt_post_code_recycle_audit`，同事务记录旧 code/queue/content 与新 queue/time | 已关闭，全量回归通过 |
| CR-F10 | P0 | 含 `{code}` 的正式 queue 使用相同 payload/idempotency_key 重试时被误判为事实冲突 | 幂等校验只在其他冻结事实完全一致时，允许 caption 等于 deterministic pre-freeze 形态或该 queue 已冻结 code 渲染后的 caption；新增 exact replay 测试，差异 payload 仍 409 | 已关闭，全量回归通过 |
| CR-F11 | P1 | 用户修改搜索输入后，旧结果和旧 `href` 仍暂时可点击，pending 响应还可能覆盖新输入状态 | 新增 input handler：输入变化立即递增请求序列、abort pending request、隐藏/清空旧结果与 href/data；过期响应受序列门禁阻止覆盖 | 已关闭，Chrome 已复验 |

## 当前检查结论

| 编号 | 检查项 | 结论 |
| --- | --- | --- |
| CR-01 | 原 `/tt` 隔离 | 旧 HTML/JS/Nginx 源文件零 diff；生产部署前后 hash 完全一致 |
| CR-02 | schema | route/audit 表、索引、trigger、queue.code 均为加法；旧表不重建 |
| CR-03 | code 约束 | code PK、大写四位 CHECK、queue unique、channel TT CHECK |
| CR-04 | 分配原子性 | queue insert、route/code、最终 caption 在同一写事务 |
| CR-05 | 碰撞/回收 | 有界随机、位图空槽、只在满池回收；无 `INSERT OR REPLACE` |
| CR-06 | `{code}` | 一次非递归、preview 不分配、所有正式 queue 冻结、直接测试拒绝 |
| CR-07 | URL 兼容 | 新正式 TT/af_dp-first；历史 pending 和直接测试 AIpost |
| CR-08 | latest clone | published-only，`published_at,created_at,queue_id` 降序，只改 channel |
| CR-09 | 公共边界 | main app 限流/gate/剧校验；private bearer sidecar；目标二次校验 |
| CR-10 | Redis 事实边界 | SQLite 写不依赖 Redis；缓存行严格验证；异常回 SQLite |
| CR-11 | 页面 | 一次组合 API、五条 Featured、drag click suppression、safe URL navigation |
| CR-12 | 测试安全 | 自动化使用临时 DB/fake Redis/fake resolver；不调用真实 publish |
| CR-13 | 幂等重放 | 相同 formal payload/idempotency_key 接受 pre-freeze 或 frozen exact caption；任何其他事实差异仍冲突 |
| CR-14 | 输入失效 | input 变化立即清空旧 CTA/href并中止 pending request；旧响应不能覆盖当前输入 |
| CR-15 | 日志短码展示 | 只读 `item.code`，四位大写字母数字才展示；空值/非法值为“—”，不从 caption 推导、不写库 |

## 最终复审完成证据

1. 在当前最终代码上确认 CR-F04 的 GET/SET/DEL 均不在共享 queue 写锁内等待网络，并运行慢 Redis 读/失效并发测试。
2. 确认 storage migration failure 会完整回滚 route/audit/queue.code 加法，不留下半迁移状态。
3. 确认 code exact 对非 published 状态仍可读取，同时公共主 app 对已下架剧 fail closed。
4. 确认所有新正式 queue（无论是否含 `{code}`）都有同一 code/route/queue.code；历史无 code queue 不被误判损坏。
5. 确认 Nginx 没有把 `/internal/tt-posts/code-resolve` 暴露，并且 `/api/public/tt-code/resolve` 只代理 8787。
6. 确认 Redis config/unit 与目标 Redis 5/systemd 239 语法兼容，6381 只监听 loopback。
7. 确认 `{code}` exact replay 只放行同一 queue 的 deterministic pre-freeze/frozen caption，不放宽其他 idempotency facts。
8. 确认输入变化清空 href 和 active result、abort pending fetch，且 race 中的旧响应无法恢复旧 CTA。
9. 确认发布任务新增列不改变统一任务 API、SQLite/Redis、队列状态与发布动作，并且 loading/empty 行同步为十列。

## 发布门禁

任何 P0/P1 finding 仍 open、最终全量自动化未通过、真实浏览器未验证、DB 副本迁移未通过、原 `/tt` 出现 diff，或验收需要真实 TikTok 发布时，均不得上线。
