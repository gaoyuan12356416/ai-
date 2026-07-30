# 018.tt-post-publish-pool 需求与技术设计

## 背景

AI 平台已有 X Post 发布池，具备素材入池、多账号排期、冻结计划、幂等发布和审计能力。现需新增独立的 TT Post 发布池：

- 个号和 Token 来源为 `ads_ai.tiktok_personal_account_snapshot`。
- 操作人员选择账号、填入素材 ID、指定北京时间并确认最终描述。
- 正片与 `20260729-113038.mp4` 固定新版片尾在 GPU 服务器数据盘加工。
- TikTok Content Posting API 的预检、发布和结果查询由 GPU 服务器发起。
- 页面、排期、素材映射、队列、日志和权限仍由 CPU 服务器承载。

TikTok 当前官方规则同时构成本需求的强制发布门禁：

- Direct Post 客户端不得只是内部团队管理自有账号的上传工具。
- API 客户端不得在内容中额外叠加品牌名、Logo、链接或推广文字。
- 未审计客户端只能发布为 `SELF_ONLY`。
- 服务端已有的视频必须使用已验证 URL Property 下的 `PULL_FROM_URL`。

因此首版交付完整发布池、GPU 成片、账号预检、队列和结果核对能力，但真实 Direct Post 默认关闭；只有审核、URL Property 和总开关三个条件均由负责人确认后，执行器才允许进入 init。

## 目标

1. 在 `ai.yingliangads.com` 新增非技术化 TT 发布池页面。
2. 安全展示快照表中的候选账号，任何前端/API/日志都不出现 Token。
3. 支持一次填写 1–100 个素材 ID，逐条解析真实 `content_id`，并按可编辑描述模板分别冻结包含正确 Drama ID 的最终描述。
4. 在 GPU `/data` 数据盘生成“去除旧 CTA 的正片 + DramaWave Logo + phone-match 过渡 + 新版片尾”的不可变成片。
5. 素材按账号进入 FIFO 发布池；账号只保存每日上海时间，到点自动消费下一条素材并生成 UTC 队列。
6. 支持“立即发布一条”，它额外消费下一条素材且不替代当天定时任务；浏览器重试、双击和定时并发都不得重复生成同一任务。
7. 在满足三重合规门禁后，由 GPU 直接调用 TikTok API；默认关闭且本需求验收不发送真实 Post。

## 范围

### 包含

- 独立权限键 `tt_posts`、导航项和 TT 发布池页面。
- 账号安全 DTO、数据库候选状态及 `creator_info` 实测状态。
- 单条/批量素材 ID 校验、素材与短剧 `content_id` 映射。
- 可编辑描述、隐私级别、互动项、商业内容披露、最终视频预览和显式同意。
- 独立 SQLite 发布池、队列、事件账本。
- 每分钟领取到期任务、账号级串行、租约、过期、取消、对账。
- GPU loopback sidecar、SSH 反向隧道、数据盘工作目录、NVENC 成片。
- TikTok `creator_info`、Direct Post init、状态查询客户端；Direct Post 受三重门禁。
- systemd 单元、配置示例、部署、回滚和测试。

### 不包含

- 修改 `tiktok_personal_account_snapshot` 或刷新其中 Token。
- 向浏览器、SQLite、文件、命令行参数或日志持久化 Token。
- 未经确认开启真实发布，或创建真实 TikTok 帖子。
- 绕过 TikTok App 审核、URL Property、隐私或内容规范。
- 自动修改 TikTok 账号隐私设置。
- 将 TT 状态写入既有 `x_post_*` 表。

## 用户故事 / 业务规则

1. 操作人员能搜索账号并看到账号名、主页、Token/账号状态、到期时间和“数据库候选/接口实测”两层状态。
2. 操作人员可一次填写 1–100 个素材 ID。页面复用 X 素材池的输入规则，支持空白、换行、中英文逗号及中英文分号分隔；正整数 ID 规范化后按首次出现顺序去重，再逐条调用既有素材预览接口。系统自动查出每项素材、真实 `content_id` 和视频预览，不允许手填或伪造 `content_id`。
3. 发布描述框首次打开即显示以下默认模板，且允许操作人员编辑：

   ```text
   Watch the full story in the app 🎬

   Drama ID: {{contect_id}}

   Visit my profile → Open the link → Search the Drama ID → Watch now.
   ```

   模板必须保留 `{{contect_id}}` 或兼容别名 `{{content_id}}`，且不得包含其他未知占位符。页面和服务端都按每项素材的真实 `content_id` 独立渲染；Core 将编辑后的模板和渲染结果一起冻结。最终描述不得超过 TikTok 的 2200 个 UTF-16 单位。
4. 隐私、评论、Duet、Stitch、商业披露和 AIGC 由“TT 个号管理”保存为账号级设置，并在保存时通过最新 `creator_info` 校验；发布池只读展示，未配置账号不能排期。
5. 用户必须看到目标账号昵称、账号级发布设置、每个成功素材的最终视频及描述规则，并显式确认音乐条款，才可排期。一次确认作用于本批成功素材，但每条任务仍分别冻结账号设置和确认信息。
6. 同一个素材在 TT 发布池和历史中全局唯一；入池时绑定账号、真实 Drama ID、最终成片和描述，之后按账号内 `created_at,id` FIFO 消费。
7. 每个账号可保存 1–24 个不重复的每日上海时间；本期页面只配置一个时间点。自动时点按“账号 + 上海日期 + 时间”唯一冻结。
8. “立即发布一条”使用独立手动请求键，额外消费下一条，不改写每日配置，也不取消同日自动时点；相同请求键精确重放只返回原任务。
9. 到点前可取消；进入 TikTok init 后只能查询/对账，不能自动重新 init。
10. init 响应不明、网络结果不明或本地提交失败时标记 `needs_review`，禁止自动重发，并阻断该账号领取下一条素材。
11. 每日时点在固定 600 秒安全宽限内按同一 run key 重试，用于承接账号串行和进程恢复；超过窗口不再生成新 run，也不追补更早的时点。
12. 三重门禁任一关闭时，允许成片、素材入池和保存每日配置，但自动/手动执行不得消费素材、不得创建可执行队列、不得请求 Direct Post init。
13. 三重门禁只代表 App/域名/总开关条件；成片还必须通过独立的素材级门禁。当前带 DramaWave Logo 和推广片尾的 profile 永久标记为 `direct_post_eligible=false`，即使三重门禁误开也不得进入 init。

## 交互与流程

1. 页面加载安全账号列表、全局发布门禁和含 `{{contect_id}}` 的可编辑默认描述模板。
2. 用户选择账号，GPU 使用短时内存 Token 调用 `creator_info`，CPU 只接收脱敏能力结果。
3. 用户批量填写素材 ID。页面先按 X 素材池分隔规则规范化、去重和限制为 1–100 项，再按输入顺序逐条调用现有 `POST /materials/preview`；单项失败只记录该项错误，不中断后续素材。
4. CPU 为每个成功项解析素材和真实 `content_id`，GPU 预生成最终视频；默认去掉原素材尾部 4.333333 秒旧 CTA，可用部署配置设为 0 关闭。预览和随后建队使用相同的确定性 prepare job 身份，GPU 可直接复用已完成产物。
5. GPU 将成片保存到 `/data/tt-post-publisher`，并按 SHA-256 固化 URL、大小、时长和探测结果。
6. 页面只读展示已保存的账号级隐私/互动/商业声明/AIGC 设置；用户编辑描述模板、设置每日上海时间、选择是否启用，并确认预览和同意条款。
7. 页面逐条调用 `POST /material-pool`。CPU 重新解析真实 `content_id`，复用确定性 GPU 成片，按当前账号冻结模板、渲染文案和同意信息；单项失败不回滚已成功项。
8. 页面用乐观版本调用 `POST /schedule` 保存账号每日时间；配置首次建立和部署迁移都默认关闭，不会因上线代码而开始发帖。
9. 每分钟 runner 先调用内部 recurring due 接口：先恢复已领取但未绑定 queue 的 run，再扫描上海时间最近 600 秒内的到期时点，以“账号 + 日期 + 时间”幂等建 run，并原子领取账号 FIFO 下一条素材。
10. “立即发布一条”先由同源后台 API 原子建立 manual run，再触发同一 runner；systemd path 唤醒失败时由下一分钟 timer 兜底。
11. CPU 在冻结 queue 前再次验证账号快照、账号设置和实时 `creator_info`。同账号已有执行中/待核对任务时不得领取第二条。
12. GPU 再次查询 `creator_info`。三重门禁和素材级门禁都通过时用已验证自有 HTTPS URL 执行 `PULL_FROM_URL`；未通过则返回合规阻断。
13. 拿到 `publish_id` 后只轮询该 ID；最终结果同步到 queue、schedule run、recurring pool 和只追加事件账本。

## 技术设计

### 影响模块

- `app.py`：页面路由、同源管理 API、权限和审计。
- `static/tt-post-pool.html`：创建任务、预览、门禁、队列和事件 UI。
- `static/quick-nav.js`、`navigation.json`：独立 TT 导航权限。
- `features/tt_posts/`：CPU 存储、账号、素材映射、调度和 GPU 客户端。
- `features/tt_gpu/`：GPU 成片、TikTok 客户端和安全 sidecar。
- `scripts/tt_post_*.py`：CPU claim/runner、GPU worker。
- `deploy/tt-post-*`、`deploy/tt-gpu-*`：systemd、timer、隧道和配置示例。

### 数据结构

`tt_post_material_pool`

- 素材 ID、真实 `content_id`、源指纹、片尾版本。
- GPU 成片 URL、SHA-256、大小、时长、MIME。
- `preparing|ready|occupied|published|validation_failed|needs_review`。

`tt_post_account_setting`

- 账号 ID、隐私、互动、商业披露、AIGC 和乐观锁版本。
- 由“TT 个号管理”维护；发布池只读消费并在每条任务中冻结快照。

`tt_post_queue`

- 幂等键、素材池 ID、快照账号 ID/名称、UTC 发布时间和时区。
- 冻结描述、隐私、互动、商业披露、AIGC 标记。
- `creator_info` 指纹、显式同意人/版本/时间。
- 租约、尝试次数、`publish_id`、远端状态、公开 Post ID、unknown 标记。

`tt_post_event`

- 只追加的阶段、状态、TikTok 安全错误码、log ID、结果是否明确和安全消息。
- 禁止存 Token、Authorization、签名上传 URL和完整请求体。

`tt_post_daily_schedule`

- 账号、固定 `Asia/Shanghai`、每日时间列表、启用状态、乐观版本、同意版本/时间和操作人。

`tt_post_recurring_pool`

- 唯一素材 ID、目标账号、真实 Drama ID、不可变 GPU 成片、描述模板/渲染结果和 FIFO 状态。

`tt_post_schedule_run`

- 自动/手动触发、唯一 run key、上海日期/时间、配置版本、素材/queue 绑定、状态与安全错误。
- 同一 run 使用 120 秒 execution lease 与每次接管重新签发的 fencing token；`freeze/release/bind` 必须在事务内核验当前 owner，过期执行者不能产生孤儿 queue 或错误释放素材。

浏览器继续逐条编排素材入池，未新增批次表。旧绝对时间 queue 与内部精确重放逻辑保留兼容；主后台不再公开精确 `POST /queue`，新页面只通过素材池和每日/手动接口创建任务。

### API / 接口

- `GET /api/admin/tt-posts/accounts`（包含不敏感的账号设置状态）
- `POST /api/admin/tt-posts/creator-info`
- `POST /api/admin/tt-posts/materials/preview`
- `GET|POST /api/admin/tt-posts/material-pool`
- `GET|POST /api/admin/tt-posts/schedule`
- `POST /api/admin/tt-posts/run-now`
- `GET /api/admin/tt-posts/queue`（仅查询；旧的精确 `POST /queue` 不再通过主后台公开代理）
- `POST /api/admin/tt-posts/queue/{queue_id}/cancel`
- `POST /api/admin/tt-posts/queue/{queue_id}/reconcile`
- `GET /api/admin/tt-posts/events?queue_id={queue_id}`

页面对规范化后的每个素材分别调用 `POST /materials/preview`，再对成功预览项分别调用 `POST /material-pool`；排期配置与手动执行是账号级接口。

内部 CPU→GPU 接口只监听 loopback 并经 SSH 反向隧道访问：

- `POST /internal/tt-post/creator-info`
- `POST /internal/tt-post/prepare`
- `POST /internal/tt-post/publish`
- `POST /internal/tt-post/reconcile`
- `GET /health`

### 异常与边界

- 快照候选条件：`is_active=1`、`account_status=2`、`token_status=2`、`disable_publish=0`、Token 非空且到期时间至少晚于执行时刻五分钟。
- 批量输入只接受 1–100 个规范化后的正整数素材 ID；重复 ID 仅保留首次出现，非法 token 在发起任何预览前整体拒绝。
- 批量预览和建队均按顺序逐条执行。素材不存在、真实 Drama ID 无效、GPU 制作失败、素材已有历史或时间冲突只影响当前项，必须继续处理后续项并返回安全明细。
- 每日时间必须为严格 `HH:MM`，固定 `Asia/Shanghai`；自动 run 以对应上海日期换算 UTC 后落库。
- TT 素材时长预校验为 `0 < duration <= 3600`；不得修改 X 渠道的 140 秒合同。最终成片仍必须不超过目标账号实时 `max_video_post_duration_sec`。
- GPU 源文件下载上限保持 2 GiB；1080x1920 规范化后的最终成片上限为 4 GiB，与 TikTok Content Posting 视频媒体限制一致，超过时仍 fail-close。
- 服务端始终以素材真实映射渲染 `caption_template`；模板必须包含 `{{contect_id}}` 或 `{{content_id}}`，未知占位符 fail-close，渲染结果以 UTF-16 单位计算且不得超过 2200。
- 旧客户端仍可只提交已经渲染的 `caption_text`；两字段均省略时继续使用当前默认模板。若同时提交 `caption_template` 和 `caption_text`，两者按真实 Drama ID 渲染后必须逐字一致。
- 精确重放旧任务即使发布时间已临近/已过，也必须在新任务时间校验和 GPU 之前返回原任务，不得改写历史模板或描述。相同幂等键只要请求中的素材、账号、时间、模板、渲染文案或确认信息任一改变，就返回幂等冲突；之后修改账号级设置不得改写或阻断历史任务重放。
- 快照表无 scope/App 审核字段，`creator_info` 成功前不得显示“接口可发”。
- Token 只在精确账号执行上下文读取，不得使用列表 SQL 读取。
- GPU 接口不接受 raw Token，只接受 AES-GCM 短时信封；AAD 绑定任务、账号和操作。
- GPU 接口不记录请求头/请求体；Token 不进入持久化对象的 `repr` 和异常。
- 正片使用用户已确认的圆角 DramaWave Logo 规格；Logo 文件 SHA 纳入成片幂等指纹。
- 正片末尾使用已确认的 0.9 秒 phone-match 过渡：裁剪后的最后净帧缩至约 `760x1352`，新版片尾在底层开始播放，正片音频淡出；不得硬切。
- 新版片尾含示例剧信息。GPU 必须明显叠加当前真实 Drama ID，并将固定画面标注为教程示例，避免把示例 ID 当成真实 ID。
- 当前制作 profile 明确包含品牌 Logo 和推广引导，只用于关闭态预览/人工工作流；GPU publish 必须读取 prepare manifest 的 `direct_post_eligible`，缺失或为 false 时在创建 publish ledger 之前拒绝。
- 服务器已有成片必须使用 `PULL_FROM_URL`，禁止以 `FILE_UPLOAD` 规避 URL Property。
- 成片 URL 必须 HTTPS、无重定向、按 SHA 不可变并处于 TikTok App 已验证路径。
- 已有 `publish_id` 的任务即使 live gate 后续关闭，仍允许安全 reconcile；reconcile 永不创建新 Post。

## 验收标准

1. 已授权人员可打开 TT 发布池，未授权人员返回 403。
2. 页面/API/日志/SQLite/测试快照中均不出现 Token 或 Authorization。
3. 账号列表与 63350 只读快照一致，Token 不合格账号不可选择。
4. 页面可一次输入 1–100 个素材 ID，按 X 素材池分隔规则规范化、去重和逐条预览；混合成功/失败时成功项可继续排期，失败项有明确安全原因。
5. 页面首屏展示可编辑默认模板；编辑后模板不会因读取或切换素材被覆盖。每条任务的 `caption_template` 与按真实 `content_id` 渲染的最终描述分别正确冻结，最终描述不超过 2200 个 UTF-16 单位。
6. 页面只设置账号每日上海时间；保存后下一次执行时间可核对，每个自然日自动 FIFO 消费一条，重复 tick 不生成第二条。
7. 前端分别调用 preview/material-pool 接口；单项失败不回滚已成功任务、不阻断后续任务，汇总数量和逐项结果可核对。
8. 手动按钮使用稳定幂等键额外消费一条并触发同一 runner；双击、网络重试及与定时器并发都不重复，同日自动配置不被改写。
9. GPU 成片只写 `/data/tt-post-publisher`，返回源/Logo/片尾/成片 SHA、大小、时长和裁剪秒数；旧 CTA 被裁掉，phone-match 过渡及音频连续；预览与入池命中同一确定性 prepare job。
9. 账号未完成发布设置、未完成显式同意、模板缺少 Drama ID 占位符、含未知/不完整占位符或任一最终描述超长时不能排期。
10. 重复素材、账号时间冲突、同一幂等键变更模板/文案、并发 claim、unknown 重发均被数据库/状态机阻止；精确重试和历史旧请求重放不产生新任务或重复制作。
11. 三重门禁默认均为 0；验收过程中 TikTok Direct Post init 调用次数为 0。
12. `creator_info` 可在不泄露 Token 的前提下返回脱敏结果；失败时 fail-close。
13. CPU/GPU 服务、timer、隧道健康检查通过，且能按各自 immutable release 回滚。

## 风险与待确认

1. 当前内部发布池用途与 TikTok Direct Post Intended Use 冲突；没有合规产品形态前不得开启。
2. 当前片尾包含 DramaWave Logo 和推广引导，与 TikTok API Watermark Guidelines 冲突；真实 Direct Post 需合规替代素材或 TikTok 书面确认。
3. 需确认开发者 App 已获 Direct Post 审核、账号 Token 具备 `video.publish`、目标域名/路径已完成 URL Property 验证。
4. 新版片尾含示例 ID，动态 Drama ID 覆盖的视觉效果需在 GPU 实片中验收。
5. 公网成片存储和保留期需在开启真实发布前确认。

## 变更记录

- 2026-07-29：初版，纳入 CPU/GPU 分工、固定新版片尾和 TikTok 官方合规门禁。
- 2026-07-29：关闭态生产部署完成；真实 GPU/COS/账号预检与后台页面通过验收，Direct Post 未开启。
- 2026-07-29：新增批量 1–100 素材编排、首条时间加可编辑间隔、可编辑描述模板、逐项部分失败隔离及历史请求兼容设计；本次变更待部署和生产关闭态验收。
- 2026-07-30：素材 ID 改为账号 FIFO 持久池；排期改为每日上海时间；新增幂等手动执行与 systemd path 唤醒；TT 独立允许最长 3600 秒，X 仍保持 140 秒。
