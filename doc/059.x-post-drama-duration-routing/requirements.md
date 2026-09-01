# 059.x-post-drama-duration-routing 需求与技术设计

## 背景

现行短剧池在排期阶段不读取真实媒体时长。目标账号不具备长视频能力时，系统用 `141s` 作为占位值并提前冻结 Premium 代发账号，导致实际不超过 140 秒的剧集也进入“会员号原帖 + 目标账号 Repost”路线；没有同语言会员号时甚至无法创建该账号的候选。

本需求只修正上线后新建的短剧池定时队列。历史精确 `141s` 队列、已发布 Post/Repost 和既有人工恢复证据不追溯、不删除、不重发。

## 目标

- 保留剧目与目标账号的固定归属，只按最终实际上传文件时长选择发布路线。
- 所有短剧池排期先冻结为逻辑路线 `duration_pending`，排期阶段不下载媒体、不选择 relay。
- 路由解析、媒体修复与会员选择均发生在 publish log、发布 Token 凭据以及任何 X 写入之前。
- 无合格 relay 时停在 `waiting_relay`，不换剧、不推进集数、零 X 写入、零 Repost ledger，并在后续自然周期重查。
- 路线一经解析永久冻结；崩溃、重启或重复请求只能读回同一路线。

## 范围

### 包含

- fixed/random 两类短剧池定时排期。
- CPU scheduler、X Sidecar/Main 共用账本、现有 GPU 媒体修复协议。
- 短剧池队列、发布日志列表、短剧集详情的路线/最终时长展示。
- 加法数据库迁移、幂等围栏、功能开关、部署与回滚证据。

### 不包含

- 素材池、人工发布、X Auto、daily/catchup/canary 的路由语义。
- GPU worker API 或修复协议变更。
- 历史 `141s` 队列迁移、历史帖子删除或重新发布。
- 为验收创建真实测试 Post/Repost。

## 用户故事 / 业务规则

| 最终成片时长 | 目标账号当前能力 | 路线 |
| --- | --- | --- |
| `<=140.000s` | 任意 | 目标账号 direct |
| `>140.000s` | 当前长视频可发布且账号公开 | 目标账号 direct |
| `>140.000s` | 不具备长视频能力 | 同语言合格会员号发原帖，目标账号 Repost |

relay 必须同时满足：当前 Token 验证成功、账号 active、approved、publish-eligible、long-video-publish-eligible、公开、与目标同语言且不是目标本人。事务内按全生命周期代发量升序、账号 ID 升序稳定选择。

边界严格使用 `<=140.0`：`139.999`、`140.000` 为 direct；`140.000001` 为长视频。

## 交互与流程

1. 排期冻结剧目、集数、目标账号与语言；逻辑路线为 `duration_pending`。
2. 自然发布请求先下载一次源文件并读取真实时长；需要修复时，原片 `<=140` 使用 standard 策略，原片 `>140` 使用 premium 策略，避免截短长片。
3. 以修复后最终文件为准，冻结 URL、SHA-256、大小、宽高和时长；本请求上传复用同一个本地文件。
4. 读取目标账号当前能力并原子解析路线。若需 relay 但暂无合格账号，队列进入 `waiting_relay` 并返回，无 publish log/ledger/Token 凭据/X 写入。
5. 后续自然周期对 waiting 队列仅重查账号能力/relay；不重复修复。如果目标账号已升级，可解析为 direct。
6. 解析完成后才能创建 publish log；relay 的 Repost ledger 与路线在同一事务生成。

## 技术设计

### 影响模块

- `scripts/x_post_schedule_runner.py`：全局短剧候选冻结、waiting 自然重查。
- `features/x_posts/publish_media_repair.py`：无日志媒体准备、一次下载与 request-local capability。
- `features/x_posts/service.py`：加法路线表、事务解析器、DB 围栏、逻辑 DTO overlay。
- `features/x_accounts/oauth_service.py`：发布日志前解析、账号实时校验、waiting 响应。
- `static/x-post-logs.html`、`static/x-post-drama-pool.html`：中文路线与最终时长。

### 数据结构

生产 `x_post_queue.delivery_mode` 有历史列级 CHECK，且核心账本被多表外键引用。为避免重建 1142 条历史队列，本实现采用加法 companion 表 `x_post_drama_delivery_route`：

- 物理 `x_post_queue.delivery_mode` 继续只保存 `direct` / `premium_relay_repost`。
- 未解析时 companion 是 `duration_pending` 或 `waiting_relay`，API 逻辑覆盖为 `delivery_mode=duration_pending`。
- resolved 后 companion 冻结最终物理路线及宽高；queue 冻结最终 URL/SHA/size/duration/repair evidence。
- 数据库触发器禁止 unresolved 路线创建 publish log，禁止 resolved 路线/relay/媒体漂移，禁止 route 删除。

该结构保持对外合同不变，并允许关闭功能开关后由兼容版本安全停放 pending/waiting 队列。

### API / 接口

- 既有 schedule plan、日志和剧集详情返回：`delivery_mode`、`route_state`、`queue_status`、`preflight_duration`、`preflight_width`、`preflight_height`、relay 信息。
- `duration_pending` 时最终时长为 0/空表示待检测。
- `waiting_relay` 发布调用返回已知非错误状态，不要求 `log_id`/`preview_url`。
- 不新增公开路由，不改变鉴权、no-store 和 Cookie 合同。

### 异常与边界

- 下载、probe、修复、指纹或尺寸漂移：在首次 X 写入前失败；保留同一短剧绑定。
- 无 relay：`waiting_relay`，错误码 `x_post_premium_relay_unavailable`，零日志/ledger。
- publish/repost unknown：沿用严格禁止自动重试，不允许改路或重选 relay。
- resolved 后账号资格变化：按冻结路线失败，不自动降级/换路。
- 功能开关关闭：不创建新 pending，已有 pending/waiting 原地停放。

## 验收标准

- 所有 approved Test Plan 用例通过，包括时长边界、短长短同剧绑定、目标升级、同语言 relay、无 relay 恢复、修复跨边界、崩溃/重复/unknown 与历史 141 回归。
- SQLite `quick_check=ok`、`foreign_key_check=0`；历史队列逐项不变。
- 任何 pending/waiting 队列均不存在 publish log 或 Repost ledger。
- 专项与全量测试不调用真实 X Post/Repost。
- GitHub-first 不可变 commit 部署；CPU scheduler、Sidecar、Main 使用同一 commit，GPU API 不变。

## 风险与待确认

- 自然首条短视频 direct 与首条长视频 relay 需要平台自然排期才能完成最终业务验收；部署健康与 mock/ledger 验证不等同平台发布成功。
- 生产当前 Reel Drama 的会员状态仅为本次快照，解析时必须实时校验。
- 回滚数据库只做兼容停放，不对已产生新路线的账本整体倒退。

## 变更记录

- 2026-09-01：需求确认；完成基线/生产只读审计；采用加法 companion route table 实现逻辑 `duration_pending`。
