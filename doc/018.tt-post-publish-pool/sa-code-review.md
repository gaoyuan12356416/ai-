# SA 代码评审

## 评审结论

最终整合版本未发现由本轮增量引入的 P0/P1/P2 问题。TT Post 发布池的批量素材、可编辑描述模板、账号设置只读消费、CPU/GPU 边界、Token 传递、幂等与未知结果保护、定时 Runner 调度顺序以及 Direct Post 关闭态均已按设计落地；既有 GPU 最小权限风险继续单独列为遗留项。

CPU 于 2026-07-29 18:48:36 CST 切换至
`/opt/tt-post/releases/5cfc657`；GPU 保持
`/opt/tt-post-gpu/releases/18148b2`。自动化测试 275/275 通过，公网与
Chrome 登录态页面验收通过；TikTok Direct Post 三项门禁始终为 0，
数据库队列为 0，未调用 TikTok 发布初始化、未创建或发布真实帖子。

本轮采用现有单项 preview/queue 路由、浏览器顺序编排和逐项失败隔离，不新增批次表。整合版本同时保留当前生产的 TT 个号设置原子批量保存能力；发布池只读消费已保存设置，未配置账号保持禁用。

## 评审范围

- `features/tt_posts/`
- `features/tt_gpu/`
- `app.py`
- `static/tt-post-pool.html`
- `static/quick-nav.js`、`static/navigation.json`
- `scripts/tt_post_*.py`、`scripts/tt_gpu_worker.py`
- `deploy/tt-*`
- TT、X 及素材状态相关自动化测试

## 关键评审结果

### 1. 发布与调度安全

- Runner 已调整为先处理到期任务的 claim/publish，再执行 reconcile，避免待调和任务积压挤占发布窗口。
- 单条发布请求异常已隔离；一条任务失败不会中断同批次后续任务。
- claim lease 为 300 秒，小于 600 秒发布宽限期，避免任务在宽限窗口内长期占用。
- 每轮 reconcile 预算固定为 5 条，限制远端状态查询对到期发布任务的影响。
- TikTok `init` 返回未知结果时不会自动重试；任务进入待调和状态，须通过状态查询或人工调和确认最终结果。
- GPU 返回的 `job_id` 会与请求值核对，避免错误关联其他转码任务。

上述 Runner P1 问题均已解决，并有回归用例覆盖。

### 2. Direct Post 关闭态

- `TT_POST_LIVE_ENABLED=0`
- `TT_POST_DIRECT_AUDIT_APPROVED=0`
- `TT_POST_URL_PROPERTY_VERIFIED=0`

三道门禁任意一道未开启即 fail-close。当前带 DramaWave 品牌片尾的媒体 manifest 同时标记 `direct_post_eligible=false`，即使全局门禁被误开，GPU 发布端仍会在创建 TikTok 发布记录前拒绝该媒体。

### 3. Token 与数据边界

- 前端与账号列表 DTO 不返回 TikTok `access_token`。
- CPU 仅在单个账号执行预检或发布时按账号精确读取 Token。
- CPU 到 GPU 使用加密凭据封装传递，Token 不写入任务 SQLite、媒体 manifest 或前端响应。
- 未发现将 Token 放入命令行参数或公开日志的实现。
- TikTok 头像 URL 带查询签名或 fragment 时由 GPU 与 CPU 双层丢弃，避免把短时媒体签名参数透传给浏览器。
- 主 API 通过独立 root-only `/etc/tt-post-app.env` 和 systemd drop-in
  取得 CPU sidecar 地址及内部 bearer；缺失时 TT 路由 fail-close，不影响主 API。

### 4. 可编辑发布描述

- 页面首屏展示当前默认模板并允许编辑，读取或切换素材不会覆盖用户编辑值。
- 合法占位符仅 `{{contect_id}}` 与 `{{content_id}}`，Service/Core 对缺失、未知、畸形和未闭合占位符 fail-close。
- Service/Core 按每个真实 `content_id` 渲染最终描述，按 UTF-16 单位执行 2200 上限，并同时冻结模板和最终文案。
- GPU 发布协议继续使用数据库中已冻结的最终描述。
- 幂等查询先识别既有任务；精确重放在 creator info/GPU 之前返回，同键改模板或文案冲突，历史 `caption_text` 与缺省模板请求仍可精确重放。

### 5. 批量素材与可编辑描述增量评审

- 页面 textarea 复用 X 素材池的空白、换行、中英文逗号/分号解析规则，规范化后限制 1–100 个唯一正整数并保持首次出现顺序。
- 页面不调用新批量端点，而是逐条调用既有 `/materials/preview` 和 `/queue`。每项异常必须局部捕获，后续项继续；页面显示逐项结果和汇总。
- 成功预览项使用“首条上海时间 + 顺序索引 × 间隔”；间隔只接受 1–1440 分钟整数，默认 10。预览失败项不占槽；建队失败项保留槽，后续项不前移。
- 描述框首屏保留当前默认模板并允许编辑，读取或切换素材不得覆盖。页面提交 `caption_template`，Service/Core 使用每项真实 `content_id` 渲染和冻结。
- 合法占位符仅 `{{contect_id}}` 与 `{{content_id}}`。最终长度在 Core 按 UTF-16 单位校验，2200 可接受、2201 拒绝。
- `caption_template` 和最终 `caption` 都属于幂等冻结身份；同 key 改模板或文案必须冲突，不能静默返回旧内容。
- 旧 `caption_text`、两描述字段均省略时的默认模板以及既有历史任务须继续精确重放；重放在 creator info/GPU 前返回，不能重新制作。
- preview 与 queue 依据相同素材身份生成确定性 prepare job；源地址、裁剪参数、Logo/片尾或 profile 变化时身份必须改变。
- 数据库同账号同一 UTC 时间和素材全局唯一约束保持不变；批量功能不能通过移除索引规避冲突。
- TikTok Direct Post 三重门禁、素材级 `direct_post_eligible=false`、Token 短时信封和 unknown 禁重发逻辑均不变。

## 问题与遗留风险

| 编号 | 级别 | 问题 | 当前控制 | 后续建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | GPU 服务目前仍以 root 运行，且为访问 NVENC 使用 `PrivateDevices=false` | systemd 已启用 `ProtectHome`、只读路径约束，并通过 `InaccessiblePaths` 阻断已知 SSH、业务 `.env` 等秘密路径 | 建立专用 GPU 服务用户和独立 Python 运行环境，完成最小权限迁移后再重新评审 | 遗留 |
| CR-002 | P2 | 预览与正式入队可能对同一素材重复转码 | preview/queue 已使用相同素材身份生成确定性 prepare job，源/profile 变化时才失效 | 保留合同测试，防止未来回归 | 已关闭 |
| CR-003 | P2 | SQLite 表结构尚无显式 schema migration/version 管理 | 当前为首版新建库，建表逻辑保持幂等 | 增加 schema version 表和逐版本迁移脚本，并纳入升级/回滚测试 | 待优化 |
| CR-004 | P2 | 批量状态仅存在浏览器，不具备服务端批次恢复 | 每条 queue 使用独立稳定幂等键；已成功任务是数据库事实，刷新后可从队列查询 | 若后续需要暂停/恢复整批，再单独设计批次账本；本轮不得隐式增加 | 已接受 |
| CR-005 | P2 | 100 个素材顺序预览可能耗时较长 | 页面显示当前项/总数并逐项失败隔离；确定性 prepare 支持重复请求复用 | 已完成浏览器交互验收；后续按真实大批量使用数据评估异步能力 | 已接受 |

## 已关闭问题

| 编号 | 原级别 | 问题 | 处理结果 |
| --- | --- | --- | --- |
| CR-CLOSED-001 | P1 | reconcile 先执行可能导致到期任务错过发布窗口 | 调整为 claim/publish 优先，reconcile 每轮最多 5 条 |
| CR-CLOSED-002 | P1 | 单条 publish 异常可能中断整个 Runner 批次 | 增加单条异常隔离并继续处理后续任务 |
| CR-CLOSED-003 | P1 | claim lease 与发布宽限窗口关系不安全 | lease 固定为 300 秒，小于 600 秒宽限期 |

## 最终编译与验证结果

- TT 测试：154/154 通过
  - Core：38
  - Service：52
  - GPU：25
  - 发布池 UI：18
  - 个号设置 UI：11
  - App contract：10
- X 回归：93/93 通过
  - X posts：29
  - Store：34
  - 多排期 UI：9
  - 素材池 UI：10
  - 账号选择器：11
- 素材状态回归：28/28 通过
- Python 编译检查：通过
- Git diff whitespace/check：通过
- 生产关闭态：公网 200/no-store；SQLite integrity ok，`material=0`、`queue=0`、`event=0`、`settings=1`
- 静态资源：TT 发布池 SHA-256 `5eb01246d3e2c8b5ba619f70ffa89132bd5879c59656fa63d3b1c5acfde68cea` 三处一致；TT 个号设置 SHA-256 `54a73f9fa26f827ff80b3e447c49ee7f62ec12c258aace9b34c4dd6dd64ce88f` 未改变
- Chrome：批量框、20 位 ID 前端拦截、可编辑默认模板、默认 10 分钟间隔、账号设置只读/未配置时建队禁用均通过，未创建任务

本轮整合版本最终结果为 `275/275`，未发现阻断上线的问题。

## 合入建议

同意本轮 CPU immutable release 上线；上线已于 2026-07-29 18:48:36 CST 完成。继续保持三项 Direct Post 门禁关闭；生产启用 Direct Post 前，仍要求平台审核、URL Property 验证、无品牌媒体策略确认和 TikTok 状态调和链路验收。

## 2026-07-30 增量代码评审（仅本地）

### 评审结论

上线与关闭态复审发现已在当前工作树完成代码关闭；其中约 5100 秒大文件失败根因仍待生产重跑确认。本轮本地自动化 TT 205/205、X 351/351（skipped 1）、素材状态 28/28，总计 584/584。最终生产通过仍以 4665764/COS 重跑、七表行数和零 Direct Post 证据为准。

### 关键实现检查

1. **时长合同隔离**
   - TT `DramawaveMaterialResolver` 使用独立的 3600 秒上限，并对类型、删除状态、URL、违规标记、短剧映射和部署时间继续 fail-close。
   - X selector 未复用 TT 上限；本地 X 回归仍固定断言查询范围 `1..140`。
   - GPU 配置样例将最大素材时长设为 3600；正式入池和生成 queue 时仍以目标账号实时 `max_video_post_duration_sec` 做最终限制。

2. **每日排期与 FIFO**
   - schedule、recurring pool、schedule run 分表保存；旧 queue 状态机不被替换。
   - 所有启用账号的 `Asia/Shanghai HH:MM` 在 `save` 的 `BEGIN IMMEDIATE` 事务内全局唯一；冲突返回 409，修改时间或禁用后释放占用；页面明确提示“不同账号需选择不同的分钟”。
   - run 对自然日时点保持唯一，pool 对账号按 `created_at,id` 领取，账号 active run 保持串行。
   - 600 秒 grace 为双端固定合同；宽限内恢复，超窗 `missed`。

3. **两段崩溃恢复**
   - `claim → freeze`：claimed run 与 reserved pool 已持久化，下一 tick 可按原 run key 恢复。
   - `freeze → bind`：legacy queue 已用 run key 冻结；bind 中断后查询既有 queue 再绑定，不重新 freeze。
   - 慢 Creator 预检不持有 120 秒 execution lease；freeze 前才 acquire。预检失败必须重新原子 acquire 当前 fencing lease 后安全 release，另有 live owner 时不得释放，旧 owner 不能 freeze。
   - 本地故障注入验证两处中断均只产生一个 run、一个 queue 和一个素材归属。

4. **手动幂等**
   - 浏览器以固定非敏感 `sessionStorage` 名保存 `account_id → {key,status}`，使用 null-prototype 对象并限制原始长度、账号数量、账号格式、key 前缀/长度/字符和状态集合。
   - 首次确认意图即落 session；成功、已提交或明确未发布只删除对应账号且 key 精确匹配的项。
   - `unknown` 与未确认响应保留原 key；页面不凭 `run_id` 单独报告成功，并区分未发布、需人工核对、已完成和已提交。
   - 前端仍不使用 `innerHTML`，映射不包含账号 Token、Authorization 或其他凭据。

5. **Runner 预算与门禁**
   - 每 tick 固定 `schedules_due(limit=1)`、`claim(limit=1)` 与 `reconciling(limit=1)`；不会批量预领或批量调和。service 返回、runner 日志透出 `deferred_count` 与 `oldest_deferred_at_utc`，避免积压静默；reconcile 多返回时 fail-closed。
   - runner generic/schedule/publish/reconcile 分别为 60/1500/2400/1500 秒，最坏远端等待 5520 秒；systemd 5700 秒保留 180 秒收尾。
   - recurring 执行在 claim 素材前检查三道门禁；关闭时不 reserve、不创建可执行 queue、不调用 TikTok init。

6. **上线前复审关闭项**
   - P0 并发时序：每个 run 增加 120 秒独占 execution lease 和 fencing token；token 不进入公开 DTO/日志，lease 到期后新 owner 获得新 token，旧 owner 的 renew/freeze/release/bind 均被拒绝。`freeze/release/bind` 在事务内核验 run、pool、lease 与 token，覆盖 release-first 和 freeze-first 两种交错。
   - P1 账号切换：未配置或加载中的账号先将发布时间恢复为 `11:00`，避免沿用上一账号值。
   - P1 公共兼容写入口：主应用精确 `/api/admin/tt-posts/queue` 方法映射改为仅 GET，删除公共创建转发；动态 cancel/reconcile 仍受同源与权限校验保护。
   - P0 长素材成片大小：TT 官方视频媒体边界为 4 GiB；源下载仍限制 2 GiB，规范化后的最终成片默认/部署上限调整为 4 GiB。
   - P1 手动 path 生命周期：`/run/tt-post` 只由常驻 sidecar 持有，oneshot runner 不再声明同名 `RuntimeDirectory`，避免退出时清理 kick 文件。
   - P1 ready manifest 当前合同：prepare 与 publish 读取 ready manifest 时均重新核验当前 `max_output_bytes` 与 profile、期望 job、已冻结 content、规范化 probe、SHA 以及由当前 COS 域名/前缀和 SHA 推导出的精确对象 URL。同一测试用 subtests 分别篡改 content/job/SHA/URL/probe/profile，并验证 publish 在 TikTok init 前 fail-close；配置收紧、身份漂移或元数据异常均不能复用旧合同结果。
   - P1 分路由超时：prepare 内部共享 8700 秒 deadline 预算，外层 CPU prepare/app exact preview/nginx exact preview 为 9000/9060/9120 秒；GPU normal 仍为 900 秒；runner generic/schedule/publish/reconcile 为 60/1500/2400/1500，systemd runner 5700。其他 API 不放宽、三项门禁不变。
   - P1 claim 租约覆盖：每次凭据读取完成后才续租；第一次续租桥接 Creator Info，第二次在 Creator 与 publish 之间续租并覆盖 TikTok init，租期为 GPU normal + 60 秒。
   - P0 recurring fencing：Creator 预检阶段不持 120 秒 execution lease；失败时重新 acquire 当前 fencing lease 后才 release，旧 owner 不能 freeze。
   - P1 账号错峰：启用账号的上海时点在同一 `BEGIN IMMEDIATE` 保存事务内全局唯一，冲突以 `tt_post_schedule_time_conflict`/409 明确拒绝，页面明确提示不同账号选择不同分钟。
   - P0 单 tick 响应边界：reconcile 响应超过请求的单条预算时直接 fail-closed，不扩大 5520 秒远端等待上界。
   - P1 大文件 COS 上传：正式合同固定 `CosConfig.Timeout=120`、`KeepAlive=false`、SDK `retry=0`，手工 multipart 每片 8MiB、每批最多 4 片；模块级共享 4 槽 `BoundedSemaphore` 约束跨 Store/批次/任务 part 并发，完成后复验 size/SHA。
   - P0 deadline 退出：prepare 从 job lock、下载、probe、转码、哈希到上传/HEAD 共用 8700 秒内部 deadline 预算；future 超时路径不等待 executor 线程退出，multipart abort 在 daemon 线程异步执行。CPU 9000 外层兜底与内部预算之间的 300 秒用于覆盖单次读/清理，不承诺严格在 8700 秒返回。
   - P0 complete unknown：complete 调用一旦开始，future 超时或结果未知时不得 abort；下次相同内容重试通过 HEAD 恢复，避免删除正在持久化的对象。
   - 生产事实与推断：关闭态约 5100 秒后本地 2.36GB 成片完成，但 manifest 仍为 1、job 为 0，COS 近期对象仅旧约 45MB 文件；失败根因尚未由正式合同重跑证实，不得标记生产通过。

### 本地验证

| 测试集 | 结果 |
| --- | ---: |
| TT Core | 49/49 |
| TT Service + Runner | 77/77 |
| TT GPU | 33/33 |
| TT 发布池 UI | 23/23 |
| TT 个号设置 UI | 11/11 |
| TT App contract | 12/12 |
| **TT 小计** | **205/205** |
| X 回归 | 351/351（skipped 1） |
| 素材状态回归 | 28/28 |
| **总计** | **584/584（skipped 1）** |

### 生产代码验收待填写

- Git 提交、远端分支、review 结论及 immutable release：`待填写`
- 部署前备份、数据库迁移、服务单元和回滚验证：`待填写`
- CPU/GPU、app、nginx、runner 与 systemd 的最终分路由 timeout：`待填写`
- GPU COS 请求 120 秒/零重试/4×8MiB/共享 4 槽 semaphore、complete unknown 不 abort、prepare 8700 秒内部预算及 2.36GB 新 COS 对象、ready manifest/job：`待重跑验证`
- timer/path、三个 `limit=1`、积压日志、reconcile 超量 fail-closed、5520+180 预算、凭据后续租与 recurring fencing：`待填写`
- 启用账号上海时点全局唯一与 409 错峰冲突：`待填写`
- 门禁关闭时 pool/run/queue 与外部请求计数：`待填写`
- 公网页面与登录态 `sessionStorage` 多账号验证：`待填写`

生产项完成前，本节不提供“同意上线”或“已上线”的结论。
