# 开发计划与完成状态

## 当前状态

业务代码、部署资产、最终全量回归、独立复审和生产部署均已完成。线上验收只使用 GET、只读状态与缓存停启验证，没有触发真实 TikTok 发布。

## 任务完成表

| 任务 | 文件 / 模块 | 实际交付 | 状态 |
| --- | --- | --- | --- |
| D1 加法迁移 | `code_routes.py`, `core.py` | route/audit 表、索引、trigger、queue `code` 字段，事务化幂等迁移 | 已完成，副本与生产验证通过 |
| D2 code 分配 | `code_routes.py` | 安全随机、有界碰撞、O(capacity) 空槽位图、满池最早回收和审计、queue 幂等 | 已完成，自动化通过 |
| D3 `{code}` | `core.py`, `service.py`, `tt-post-pool.html` | 正式队列冻结、preview `A1B2`、UTF-16 校验、直接测试拒绝 | 已完成，自动化通过 |
| D4 正式 URL | `links.py`, `core.py`, `service.py` | 新正式队列 TT、`af_dp` 第一、冻结 route；历史/直接测试 AIpost 兼容 | 已完成，自动化通过 |
| D5 Redis | `code_routes.py`, env/deploy | stdlib RESP、24h/30s 固定 TTL、随机 namespace、SQLite fallback、loopback 6381 | 已完成，生产降级验证通过 |
| D6 公共组合 API | `app.py`, `service.py` | 主 app 限流/并发/剧目校验，bearer 私有 sidecar，一次组合响应 | 已完成，线上验证通过 |
| D7 新页面 | `tt-drama-code-search.html/js` | code/ID 搜索、五条 Featured、触摸/鼠标/触控笔/按钮横滑和防误触 | 已完成，移动与桌面验证通过 |
| D8 部署资产 | Nginx、Redis config/unit、env examples | `/tt-code`/JS/API exact routes，Redis 独立 unit，原 `/tt` 不动 | 已完成生产部署 |
| D9 自动化 | `scripts/test_tt_*` | allocator/schema/cache/API/link/UI/bridge 及既有 TT 回归 | 已完成：Python 395、Node 84/53 |
| D10 文档与上线证据 | 本目录 | 需求/API/评审/测试/部署合同及生产证据 | 已完成 |
| D11 发布任务短码列 | `static/tt-post-pool.html`、UI/服务合同测试 | 自动任务显示冻结的四位大写字母数字短码；无值或非法值显示“—” | 已完成开发，待上线复验 |

## 最终实现顺序

1. 在既有 `ensure_storage()` 的加法迁移尾部开启新的 `BEGIN IMMEDIATE`，原子创建 route/audit/trigger 并增加 queue code 字段。
2. queue freeze 预判自增 queue ID，构造正式 TT URL并插入 queue；随后在同一写事务内分配 route code、渲染最终 caption、更新 queue.code。
3. route 状态由 queue 状态 trigger 同步；published 时设置 `published_at` 并失效同剧 latest 缓存。
4. sidecar resolver 先读 Redis，任何异常回退 SQLite；route 写入和 code 唯一性从不依赖 Redis。
5. Nginx 把公共 exact route 交给主 app 8787；主 app 调 bearer-protected sidecar、验证剧目与 target，再合并一个 item。
6. 前端只发起一次组合 resolver 请求；Featured 初始卡片仍来自既有 Featured JSON。

## 关键算法

### code allocator

- `BEGIN IMMEDIATE` 内先按 `queue_id` 检查幂等。
- 最多 128 次安全随机候选；只选数据库未占用 code，以普通 `INSERT` 写入。
- 随机未找到且容量未满时，一次读取占用 code 构造 bytearray 位图，确定性取第一个空槽，不执行逐 code SQL 查询。
- 只有 `COUNT(*) >= capacity` 时才按 `created_at ASC, code ASC` 删除最早 route、复用其 code，并写 `tt_post_code_recycle_audit`。

### 最新 published clone

- 精确过滤 `content_id` 和 `state='published'`。
- 排序为 `published_at DESC, created_at DESC, queue_id DESC`。
- 只替换 `af_channel`，按 `af_dp` 第一的统一 encoder 重建 URL；原 row 不写入。

### Redis 降级

- cache namespace 是每个 sidecar 进程生成的随机值；key 使用 identity 的 SHA-256，不暴露业务 ID。
- 正缓存 86400 秒，负缓存 30 秒；当前为代码常量，不是 env。
- cache row 只有在字段集合、code/content ID、published state、TT channel 和完整冻结 URL 全部一致时才使用。
- miss/超时/协议/JSON/字段异常读 SQLite；namespace 在读前后不一致则重试，连续变化后做有界 SQLite 最终读取。
- route 写入先旋转 namespace，使所有旧 key 立即不可达；定向 code/latest 失效先旋转，再在锁外 best-effort DELETE 对应旧 key。SQLite 始终是事实源。

## 兼容决策

- 所有新正式队列都生成 code，即使 caption 没有 `{code}`。
- `{code}` 仅正式队列；直接测试返回稳定错误。
- 新正式 route 使用 `af_channel=TT` 和 `af_dp` 第一。
- 历史无 code pending queue 与直接测试保留 `AIpost` 和历史参数顺序。
- 直接 ID/Featured 无 published route 时使用 `af_dp,c=TTpost,af_c_id=0001,af_channel=source`。

## 本地完成门禁

交付生产前必须在当前最终 diff 上重新完成：

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

另需在隔离浏览器验证 390x844 和桌面视口的五条卡片、按钮、鼠标拖动、snap、code 大写和无 console error。

## 完成记录

- 运行代码 exact commit：`b01dabe22d9da1571c68b6fb0775a61bb48e18de`。
- 服务器 release、DB 副本迁移、备份 manifest、Redis/Nginx/systemd、公共 API、浏览器和旧 `/tt` 隔离均已验证。
- 发布前后 queue、run、plan、publish ID 计数一致；没有调用 publish、canary、`run-now` 或人工 scheduler。
- 2026-08-05 增量仅调整发布任务表格展示和合同测试；不改变 DB、Redis、队列冻结、发布或跳转链路。
