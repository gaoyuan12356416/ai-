# 测试报告

## 测试结论

最终整合版本自动化测试共执行 275 项，275 项全部通过：

- TT Post：154/154
- X 发布池回归：93/93
- 素材状态回归：28/28

Python 编译检查与 Git diff check 均通过。当前结果证明批量素材、可编辑描述模板、账号设置只读消费、核心状态机、CPU/GPU 协议、Runner 调度、页面契约和既有功能回归符合预期。

生产关闭态验收同时通过。CPU 于 2026-07-29 18:48:36 CST 切换至 `/opt/tt-post/releases/5cfc657`；GPU 保持 `/opt/tt-post-gpu/releases/18148b2` 未改。公网页面返回 200 且 `Cache-Control: no-store`；TikTok Direct Post 三重门禁全程为 0，数据库队列保持为 0，验收未创建任务、未初始化或发布真实 Post。

## 测试范围

- TT Core：任务状态、参数冻结、幂等、时间转换、发布门禁和未知结果保护
- TT Service：账号与素材读取、预检、入队、取消、调和、GPU 协议和 Runner 行为
- GPU Worker：媒体准备、manifest、凭据封装、Direct Post 门禁、发布与调和协议
- TT 发布池 UI：批量素材、账号选择、素材预览、发布时间间隔、可编辑描述模板、同意确认和逐项状态展示
- TT 个号设置 UI：只读消费已保存设置、未配置禁用及原子批量设置能力回归
- App contract：路由、权限、内部服务调用和前端静态资源契约
- X 发布池回归：既有发布逻辑、存储层和 UI
- 素材状态回归：webhook 与广播逻辑

## 执行统计

| 测试集 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: |
| TT Core | 38 | 0 | 0 |
| TT Service | 52 | 0 | 0 |
| TT GPU | 25 | 0 | 0 |
| TT 发布池 UI | 18 | 0 | 0 |
| TT 个号设置 UI | 11 | 0 | 0 |
| TT App contract | 10 | 0 | 0 |
| **TT 小计** | **154** | **0** | **0** |
| X posts 回归 | 29 | 0 | 0 |
| X store 回归 | 34 | 0 | 0 |
| X 多排期 UI 回归 | 9 | 0 | 0 |
| X 素材池 UI 回归 | 10 | 0 | 0 |
| X 账号选择器回归 | 11 | 0 | 0 |
| **X 小计** | **93** | **0** | **0** |
| 素材状态广播回归 | 13 | 0 | 0 |
| 素材状态 Webhook/App 回归 | 15 | 0 | 0 |
| **素材状态小计** | **28** | **0** | **0** |
| **总计** | **275** | **0** | **0** |

## 重点验证结果

### Runner 调度与容错

- 到期任务按 claim/publish 优先处理，reconcile 在其后执行。
- 单条 publish 抛出异常时，Runner 记录该条错误并继续处理后续任务。
- claim lease 为 300 秒，严格小于 600 秒发布宽限期。
- 每轮 reconcile 预算为 5 条，避免待调和积压影响发布窗口。
- GPU 返回错误 `job_id` 时请求失败，不会将错误产物关联到任务。
- `tt_media_profile_not_direct_post_eligible` 被识别为明确的“未创建远端发布”结果，不进入未知结果重试路径。

### Direct Post 门禁

- 三个全局门禁默认均为关闭：
  - `TT_POST_LIVE_ENABLED=0`
  - `TT_POST_DIRECT_AUDIT_APPROVED=0`
  - `TT_POST_URL_PROPERTY_VERIFIED=0`
- 当前品牌片尾媒体 manifest 为 `direct_post_eligible=false`。
- 自动化测试确认关闭门禁或媒体不合规时均会在 TikTok 发布初始化前 fail-close。
- 带查询签名或 fragment 的 TikTok 头像 URL 不会从 GPU/CPU DTO 透传到浏览器。

### 批量素材与可编辑发布描述

- 素材框支持空白、换行、中英文逗号及分号分隔，规范化后按首次出现顺序去重，并限制为 1–100 个唯一素材 ID。
- 前端在请求前拒绝规范化后超过 19 位的 ID；Chrome 验收已确认 20 位 ID 不发起读取请求。
- preview 和 queue 均逐项执行；单项失败不会中断后续项，结果区展示每个素材的成功或失败状态。
- 首条时间按 Asia/Shanghai 保存，间隔为 1–1440 分钟整数，默认 10 分钟；预览失败不占时间槽，建队失败后续项不前移。
- 页面描述框可编辑，首屏展示当前默认模板；支持 `{{contect_id}}` 与 `{{content_id}}`，服务端按每个素材真实 `content_id` 渲染并冻结最终描述。
- 缺失、未知、畸形或未闭合占位符均被拒绝；最终文案按 UTF-16 单位执行 2200 上限校验。
- 精确幂等重放在 creator info 和 GPU 之前返回；同键修改模板产生冲突，旧 `caption_text` 和历史任务精确重放继续兼容。
- preview 与 queue 使用确定性的 prepare 身份，源素材或媒体 profile 改变时身份才变化。
- Chrome 验收确认页面默认显示并允许编辑下列模板：

```text
Watch the full story in the app 🎬

Drama ID: {{contect_id}}

Visit my profile → Open the link → Search the Drama ID → Watch now.
```

### 回归验证

- X 发布池 93 项既有测试全部通过，未发现 TT 新功能对 X 路由、存储或页面造成回归。
- 素材状态 webhook 与广播共 28 项测试全部通过。
- Python 编译检查通过。
- Git diff whitespace/check 通过。

## 缺陷情况

本轮测试未发现 P0 缺陷。Runner 相关 P1 已关闭：

- claim/publish 执行顺序已修复；
- publish 单条异常已隔离；
- lease 调整为 300 秒；
- reconcile 预算限制为 5 条。

## 遗留风险

- P1：GPU Worker 目前仍以 root 运行，且 `PrivateDevices=false`。当前通过 `ProtectHome`、只读目录和 `InaccessiblePaths` 阻断已知秘密路径；后续应迁移至专用服务用户和独立运行环境。
- P2：SQLite 尚缺少显式 schema version 与迁移框架，后续升级前需补齐。
- Direct Post 平台审核、URL Property 验证以及品牌片尾的合规路径均未完成，因此不得开启真实发布。

## 生产关闭态验收

- CPU release：`/opt/tt-post/releases/5cfc657`
- 切换时间：2026-07-29 18:48:36 CST
- 上一 CPU release：`/opt/tt-post/releases/779ac3b`
- GPU release：`/opt/tt-post-gpu/releases/18148b2`
- CPU 更新前备份：`/root/tt-post-backups/20260729T183935+0800-9fd6431-batch-caption`。备份目录名含 `9fd6431`，但其中 `current` 实际捕获的是切换前在线 release `/opt/tt-post/releases/779ac3b`。
- TT 发布池静态页 SHA-256 `5eb01246d3e2c8b5ba619f70ffa89132bd5879c59656fa63d3b1c5acfde68cea`，release、主服务静态目录和 nginx 三处一致。
- TT 个号设置页 SHA-256 `54a73f9fa26f827ff80b3e447c49ee7f62ec12c258aace9b34c4dd6dd64ce88f`，本次部署前后未改变；既有“TT 个号设置原子批量保存”能力完整保留。
- SQLite `PRAGMA integrity_check=ok`；`material=0`、`queue=0`、`event=0`、`settings=1`。
- 三项 Direct Post 门禁均为 `0`。
- 公网页面 `/tt-post-pool.html` 返回 200 且 no-store。
- Chrome 登录态验收通过：批量框可用，20 位 ID 被前端拦截，当前默认模板可见且可编辑，排期间隔默认 10 分钟；发布池只读展示账号设置，账号未配置设置时建队按钮禁用。
- 浏览器验收未创建队列任务；数据库 `queue=0`，未调用 TikTok 发布初始化，也未发布帖子。
- GPU current 保持 `/opt/tt-post-gpu/releases/18148b2`，本次未切换 GPU release。

## 发布建议

当前仅允许继续运行三道 Direct Post 门禁关闭的准备/预检版本。完成 TikTok 平台审核、URL Property 验证与无品牌媒体合规确认之前，不得开启真实 Direct Post。

## 2026-07-30 增量测试报告（仅本地自动化）

本节是对 2026-07-30 增量代码的本地结果，不更新也不继承上文 2026-07-29 的生产 release 结论。当前增量尚未在本报告中记录生产部署、线上页面通过或真实 TikTok 发布通过。

### 执行结果

新 profile 全量本地自动化结果为 TT 212/212、X 351/351（skipped 1）、素材状态 28/28，总计 591/591（skipped 1）。该结果覆盖默认 `tt-post-hevc-720x1280-v2`、H.264 兼容回退、正片单次完整编码、跨 profile 隔离、GPU 下载前 profile 握手、CPU 响应复验及品牌资产哈希复验：

| 测试集 | 结果 |
| --- | --- |
| TT Core | 49/49 |
| TT Service + Runner | 78/78 |
| TT GPU | 39/39 |
| TT 发布池 UI | 23/23 |
| TT 个号设置 UI | 11/11 |
| TT App contract | 12/12 |
| **TT 小计** | **212/212** |
| X 回归 | 351/351（skipped 1） |
| 素材状态回归 | 28/28 |
| **完整回归合计** | **591/591（skipped 1）** |

Python 编译检查与 Git diff check 均通过。以上为本地代码和合同证据；34.8 分钟完整生产成片、COS 对象、ready manifest/job 仍待重跑，真实 TikTok 发布未执行。

执行命令：

```text
python -m unittest scripts.test_tt_posts_core scripts.test_tt_posts_service scripts.test_tt_gpu_worker scripts.test_tt_post_pool_ui scripts.test_tt_account_settings_ui scripts.test_tt_posts_app_contract
python -m unittest discover -s scripts -p "test_x*.py"
python -m unittest scripts.test_material_status_broadcast scripts.test_material_status_webhook_app
```

### 上线前复审发现并关闭

| 级别 | 复审发现 | 本地关闭方案与证据 | 状态 |
| --- | --- | --- | --- |
| P0 | 同一 run 并发执行时，一个执行者预检报错释放素材，另一个执行者仍可能冻结 queue，形成错误释放或孤儿 queue | 增加每个 run 独占的 120 秒 execution lease 与不可外泄的 fencing token；`freeze/release/bind` 均在事务内核验 run、pool、lease 与 token 身份；本地覆盖 release-first、freeze-first、lease 到期接管及过期 owner 拒绝 | 本地关闭 |
| P1 | 从已配置账号切到未配置/加载中账号时，时间控件可能沿用上一账号时间 | 未配置或加载态先重置为默认 `11:00`，再按当前账号数据渲染；本地页面合同测试通过 | 本地关闭 |
| P1 | 主应用公共兼容 `POST /queue` 可绕过新入口，在门禁关闭时 reserve 素材 | 主应用精确 `/api/admin/tt-posts/queue` 方法白名单改为仅 `GET`；保留 GET 查询及动态 cancel/reconcile，移除公共兼容写入转发 | 本地关闭 |
| P0 | 4665764 旧成片约 2.36GB/2.2GB，曾被当作“只需放宽到 4 GiB”的合理长素材产物 | 已确认根因是 `CQ20 + 8M/10M`、720→1080 放大和正片两次完整编码。4 GiB 只保留为硬安全上限；正常交付必须低于 500 MB，34.8 分钟默认 HEVC 预计约 295 MB、H.264 回退预计约 433 MB | 本地合同通过；完整生产成片待重跑 |
| P1 | sidecar 与 oneshot runner 同时声明 `/run/tt-post`，runner 退出后可能清理手动 kick 目录 | 运行目录只由常驻 sidecar 持有，runner 复用目录但不声明所有权；部署合同测试固定该约束 | 本地关闭，生产复测中 |
| P1 | ready manifest 生成后配置收紧或媒体身份漂移时，旧缓存可能绕过当前输出合同 | prepare/publish 共用当前合同复验；同一测试以 subtests 实际篡改 content/job/SHA/精确 URL/probe/profile，并验证 publish 在 TikTok init 前返回 `prepared_media_invalid` | 本地关闭 |
| P1 | prepare 共用普通 GPU 900 秒窗口会提前超时；长素材准备需要端到端总预算而不能把单个 COS 请求无限放大 | prepare 全流程共享 8700 秒内部 deadline 预算，外层 CPU/App/nginx 为 9000/9060/9120；COS 单请求 120 秒、SDK retry=0；GPU 普通 900。runner 预算保持不变 | 本地关闭，生产复测中 |
| P1 | 批量预领使后续 queue 的 300 秒 lease 在前一条慢发布期间过期；慢凭据读取与 Creator Info 也可能耗尽已有 lease | 每个 tick 对 schedule、claim 和 reconcile 均只取 1 条；`publish_claimed` 在每次读取凭据后续租到 GPU 普通超时 + 60 秒，第一段覆盖 Creator Info，第二段衔接 TikTok init/GPU publish | 本地关闭 |
| P0 | recurring 慢 Creator 预检若占用 120 秒 execution lease，预检返回时可能已失权；失败分支若沿用旧 token 释放，还可能释放新 owner 的素材 | recurring Creator 预检不持有 execution lease；成功后原子取得当前 fencing lease，失败释放前也必须重新取得并核验当前 owner；若已有其他 live owner 则不释放，旧 owner 无法 freeze | 本地关闭 |
| P1 | 多个启用账号配置相同每日时点会在同一分钟竞争 Runner 预算，无法保证既定发布时间 | 所有启用账号的 Asia/Shanghai `HH:MM` 在保存的 `BEGIN IMMEDIATE` 事务中全局唯一；冲突返回 `tt_post_schedule_time_conflict` / HTTP 409，页面明确提示“不同账号需选择不同的分钟”，改时或停用释放旧时点 | 本地关闭 |
| P0 | sidecar 若返回超过请求上限的 reconcile 列表，会突破单 tick 预算；`limit=1` 若无积压字段又会让等待任务静默 | reconcile 超量响应直接 fail-closed；service 返回并由 runner 日志透出 `deferred_count`、`oldest_deferred_at_utc`，5520 秒远端等待上界不变 | 本地关闭 |
| P1 | 旧 profile 产物体积异常，或继续复用旧/异 profile ready job，会污染新链路 | 默认 profile 升级为 `tt-post-hevc-720x1280-v2` 并纳入 job/manifest 身份，保持原生 720P HEVC/H.265、VBR900k/max1350k/buf1800k、AAC128k；H.264 1500k/max2200k/buf3000k、AAC128k仅作为 `tt-post-h264-720x1280-v2` 兼容回退。两者正片都只完整编码一次，旧/异 profile job 不复用 | 本地自动化通过 |
| P0 | GPU prepare 返回的 profile 与 CPU 当前预期发生漂移时，错误产物可能继续被冻结或发布 | CPU 独立校验 GPU prepare 响应 profile；不一致时返回 `tt_prepared_media_profile_mismatch`，不继续创建可执行发布链路 | 本地自动化通过 |
| P1 | 仅在 GPU 返回后发现 profile 漂移，会浪费源下载与制作资源 | CPU prepare 请求强制携带 `expected_profile`；GPU 在下载和制作前握手，不一致时返回 `prepare_profile_mismatch`，下载调用和 manifest 均为 0 | 本地自动化通过 |
| P1 | ready job 只信任首次 Logo/片尾 SHA 时，品牌资产更新后可能静默复用旧成片 | 每次 ready 复用重新读取并哈希当前 Logo 和固定片尾；任一变化均返回 `prepare_idempotency_conflict` | 本地自动化通过 |
| P0 | future 超时后若等待线程池退出，或同步等待 multipart abort，会让 HTTP 请求突破 9000/9060/9120 外层预算 | 超时路径 `shutdown(wait=false)`，不等待 SDK 线程退出；multipart 通过 daemon 线程异步 abort。8700 是内部预算，CPU 9000 的 300 秒余量覆盖单次读/清理，不承诺严格在第 8700 秒返回 | 本地关闭，生产待验证 |
| P0 | complete future 超时后远端结果未知，此时 abort 可能删除正在持久化的对象 | complete 一旦开始便不 abort；稍后完成时，同内容重试通过 HEAD 恢复且不创建第二次 multipart | 本地关闭 |
| P1 | 每批最多 4 片仍不能约束并发 prepare 或多个 Store 的进程内 part 总量 | 模块级共享 4 槽 `BoundedSemaphore`，跨 Store、批次与并发任务统一限流 | 本地关闭 |

### 已由最近基线自动化证明的增量事实

- 以素材 4665764 的 2087 秒属性构造的本地 fixture 可通过 TT `1..3600` 秒 resolver 合同；X selector 隔离合同另有本地回归固定 SQL 参数为 `1,140`。
- GPU 源文件默认上限保持 2 GiB，最终成片 4 GiB 硬安全上限和超限 fail-close 已有自动化覆盖；该上限不代表交付体积合格。
- ready manifest 命中时仍按当前合同重新核验输出上限与 profile、job/content 身份、规范化 probe、SHA 和精确 COS 对象 URL；篡改任一字段均失败，publish 不会在校验失败后调用 TikTok init。
- CPU 对 GPU prepare 响应再做 profile 对等校验；`test_prepare_rejects_gpu_profile_drift` 已证明 GPU 返回 H.264 回退 profile、CPU 期望默认 HEVC 时会以 `tt_prepared_media_profile_mismatch` fail-close。
- prepare 请求强制包含 `expected_profile`；`test_prepare_rejects_profile_mismatch_before_download` 已证明 GPU 在源下载和制作前拒绝漂移，且不写 manifest。
- `test_prepare_reuse_rejects_changed_logo_or_outro_assets` 已证明 ready 复用会重新哈希当前 Logo/片尾，变更时以 `prepare_idempotency_conflict` 拒绝旧成片。
- prepare 全流程共享 8700 秒内部 deadline 预算；CPU prepare 9000 是外层兜底，300 秒余量覆盖单次读/清理，主应用 exact preview 9060、nginx exact preview 9120。COS 每请求 120 秒、SDK retry=0、每片 8MiB、每批最多 4 片，并由模块级共享 4 槽 semaphore 限制进程内 part 总并发；future 超时路径不等待 executor 并异步 abort，但 complete outcome unknown 不 abort、重试经 HEAD 恢复。GPU 普通 900，runner 通用 60/schedule 1500/publish 2400/reconcile 1500，systemd runner 5700；每个 tick 最坏远端等待仍为 5520 秒。
- `/run/tt-post` 只由常驻 sidecar 的 systemd `RuntimeDirectory` 持有，oneshot runner 不再重复声明同名目录。
- 每日设置与待发素材分开持久化；同一自然日时点幂等，账号级 FIFO 和账号隔离成立。所有启用账号的 Asia/Shanghai `HH:MM` 在保存的 `BEGIN IMMEDIATE` 事务中全局唯一，冲突返回 `tt_post_schedule_time_conflict` / HTTP 409，页面明确提示“不同账号需选择不同的分钟”，改时或停用会释放旧时点。
- 发布宽限固定为 600 秒；非 600 配置被拒绝，宽限内恢复与超窗 `missed` 均有自动化覆盖。
- `claim → freeze` 中断后可在后续 minute tick 找回 claimed run；`freeze → bind` 中断后按稳定 queue 幂等键找回既有 queue。两类恢复均未创建重复 run/queue 或重复消费素材。
- 同一 run 的执行权由 120 秒独占 lease 和 fencing token 约束；recurring 慢 Creator 预检不占用该 lease，成功后才原子取得当前 fencing lease，失败释放前也须重新取得并核验 owner；过期 owner 不能 freeze，也不能释放新 owner 的素材。
- 切换到未配置或加载中的账号时，发布时间恢复默认 `11:00`，不会继承上一账号的值。
- 主应用公共精确 `/queue` 路由只读；门禁关闭时不能通过兼容 POST reserve 素材，GET/cancel/reconcile 能力保持。
- 手动请求 key 按账号保存到固定、非敏感的 `sessionStorage` 映射；读取时校验账号格式、key 前缀/长度/字符、状态、总长度和账号数量。成功或明确未发布删除对应账号，`unknown` 或未确认结果保留。
- Runner 每个 tick 依次执行 `schedules_due(limit=1)`、`claim(limit=1)`、`reconciling/reconcile limit=1`；service 返回并由 runner 日志透出 `deferred_count` 与 `oldest_deferred_at_utc`，避免限流积压静默；reconcile 多返回时 fail-closed，慢任务不会让后续 queue 提前持有 lease，5520 秒远端等待上界不变。
- queue 初始 claim lease 仍为 300 秒（小于 600 秒 grace）；`publish_claimed` 每次读取凭据后续租到 GPU 普通超时 + 60 秒，第一段覆盖 Creator Info，第二段衔接 TikTok init/GPU publish。
- 三项门禁任一关闭时，手动发布在领取素材前 fail-close；本地断言素材仍为 `available`、queue 为空且 publish 调用为 0。

### 生产关闭态长素材实跑发现

- 已确认事实：旧约 2.36GB/2.2GB 成片是 `CQ20 + 8M/10M`、720P 放大到 1080P并对正片做两次完整编码造成的配置异常，不是合理成片或可接受的待上传交付物。
- 默认新方案：profile=`tt-post-hevc-720x1280-v2`，原生 720 × 1280 HEVC/H.265，VBR900k/max1350k/buf1800k、AAC128k，正片主时长只完整编码一次；4 GiB 仅为硬安全上限。
- 兼容回退：profile=`tt-post-h264-720x1280-v2`，原生 720 × 1280 H.264，VBR1500k/max2200k/buf3000k、AAC128k。仅在兼容性门禁命中时使用，并生成独立 job/manifest，不得与默认 HEVC 跨 profile 复用。
- 已验证样片：同一 60 秒样片中默认 HEVC VMAF 89.79、H.264 回退 VMAF 90.24；HEVC 样片已在当前后台链路与 Chrome 151 完整播放，TikTok 官方媒体规格支持 H.265。该证据不等于目标账号 Direct Post 或完整生产成片已通过。
- 待验证：34.8 分钟默认 HEVC 预计约 295 MB，H.264 回退预计约 433 MB，交付必须低于 500 MB。当前尚无新 profile 的完整生产实测文件、新 COS 对象或 ready manifest/job，状态保持“待重跑”；旧上传失败的具体错误链也不得从异常体积反推为已证实。

### 尚未完成的生产验收

| 验收项 | 结果 |
| --- | --- |
| Git 提交、远端分支和不可变 release 对应关系 | 待填写 |
| CPU/GPU 备份、部署、服务重启与回滚演练 | 待填写 |
| 生产 SQLite 七表增量迁移、完整性和真实行数 | 待填写 |
| 生产 timer/path 唤醒、600 秒宽限、schedule/claim/reconcile 各限 1 条、积压日志、reconcile 超量 fail-closed、5520 秒远端等待上界及 5700 秒总预算 | 待填写 |
| 慢凭据续租、recurring 慢 Creator fencing 接管与旧 owner 拒绝 | 待填写 |
| 启用账号 Asia/Shanghai `HH:MM` 全局唯一、409 冲突与改时/停用释放 | 待填写 |
| 三项门禁关闭时 pool/run/queue 不消费证据 | 待填写 |
| 素材 4665764 真实 preview、成片时长和目标账号 3600 秒能力 | 待填写 |
| 默认 `tt-post-hevc-720x1280-v2` 媒体参数、H.264 兼容回退、正片单次完整编码、跨 profile job 隔离、双向 profile 校验及品牌资产哈希复验 | 本地自动化通过 |
| 34.8 分钟默认 HEVC 约 295 MB预计值、低于 500 MB交付、新 COS 对象及 ready manifest/job | 待重跑验证；H.264 回退约 433 MB仅为样片推算 |
| 登录态页面的每日排期、FIFO 数量、跨刷新/多账号手动幂等 | 待填写 |
| 公网 no-store、静态资源 hash 和 TikTok 外部请求计数 | 待填写 |

在上述项目填写并复核前，本节不得作为“已生产部署”或“线上通过”的依据。
