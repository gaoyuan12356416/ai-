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
3. 根据素材 ID 自动解析真实 `content_id`，冻结包含 Drama ID 的最终描述。
4. 在 GPU `/data` 数据盘生成“去除旧 CTA 的正片 + DramaWave Logo + phone-match 过渡 + 新版片尾”的不可变成片。
5. 以北京时间排期、UTC 落库，任务可追踪、可取消、不可重复发布。
6. 在满足三重合规门禁后，由 GPU 直接调用 TikTok API；默认关闭且本需求验收不发送真实 Post。

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
2. 操作人员输入素材 ID 后，系统自动查出素材、真实 `content_id` 和视频预览，不允许手填或伪造 `content_id`。
3. 默认描述为：

   ```text
   Watch the full story in the app 🎬

   Drama ID: <content_id>

   Visit my profile → Open the link → Search the Drama ID → Watch now.
   ```

   UI 可编辑描述，但保存时必须包含当前真实 `content_id`。
4. 账号隐私选项只能来自最新 `creator_info`，且无默认值；评论、Duet、Stitch 默认均关闭。
5. 用户必须看到目标账号昵称、最终视频/描述预览，并显式确认音乐条款和商业内容披露，才可排期。
6. 同一个素材在全局只能被一个未取消/未失败任务占用；同一账号同一发布时间只能有一个任务。
7. 到点前可取消；进入 TikTok init 后只能查询/对账，不能自动重新 init。
8. init 响应不明、网络结果不明或本地提交失败时标记 `needs_review`，禁止自动重发。
9. 超过执行宽限窗口的任务标记 `missed`，不补发。
10. 三重门禁任一关闭时，允许创建、成片、预检和排期，但到点状态必须停在 `blocked_compliance`，不得请求 Direct Post init。
11. 三重门禁只代表 App/域名/总开关条件；成片还必须通过独立的素材级门禁。当前带 DramaWave Logo 和推广片尾的 profile 永久标记为 `direct_post_eligible=false`，即使三重门禁误开也不得进入 init。

## 交互与流程

1. 页面加载安全账号列表和全局发布门禁。
2. 用户选择账号，GPU 使用短时内存 Token 调用 `creator_info`，CPU 只接收脱敏能力结果。
3. 用户填素材 ID，CPU 解析素材和 `content_id`，GPU 预生成最终视频；默认去掉原素材尾部 4.333333 秒旧 CTA，可用部署配置设为 0 关闭。
4. GPU 将成片保存到 `/data/tt-post-publisher`，并按 SHA-256 固化 URL、大小、时长和探测结果。
5. 用户选择隐私/互动/商业声明，编辑描述、选择北京时间并确认预览和同意条款。
6. CPU 原子创建队列任务并冻结所有字段。
7. 每分钟 runner 领取到期任务；CPU 再次验证账号快照并将一次性任务信封发送给 GPU。
8. GPU 再次查询 `creator_info`。三重门禁通过时用已验证自有 HTTPS URL执行 `PULL_FROM_URL`；未通过则返回合规阻断。
9. 拿到 `publish_id` 后只轮询该 ID；最终结果和安全错误写入事件账本。

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

`tt_post_queue`

- 幂等键、素材池 ID、快照账号 ID/名称、UTC 发布时间和时区。
- 冻结描述、隐私、互动、商业披露、AIGC 标记。
- `creator_info` 指纹、显式同意人/版本/时间。
- 租约、尝试次数、`publish_id`、远端状态、公开 Post ID、unknown 标记。

`tt_post_event`

- 只追加的阶段、状态、TikTok 安全错误码、log ID、结果是否明确和安全消息。
- 禁止存 Token、Authorization、签名上传 URL和完整请求体。

### API / 接口

- `GET /api/admin/tt-posts/accounts`
- `POST /api/admin/tt-posts/creator-info`
- `POST /api/admin/tt-posts/materials/preview`
- `GET /api/admin/tt-posts/queue`
- `POST /api/admin/tt-posts/queue`
- `POST /api/admin/tt-posts/queue/{queue_id}/cancel`
- `POST /api/admin/tt-posts/queue/{queue_id}/reconcile`
- `GET /api/admin/tt-posts/events?queue_id={queue_id}`

内部 CPU→GPU 接口只监听 loopback 并经 SSH 反向隧道访问：

- `POST /internal/tt-post/creator-info`
- `POST /internal/tt-post/prepare`
- `POST /internal/tt-post/publish`
- `POST /internal/tt-post/reconcile`
- `GET /health`

### 异常与边界

- 快照候选条件：`is_active=1`、`account_status=2`、`token_status=2`、`disable_publish=0`、Token 非空且到期时间至少晚于执行时刻五分钟。
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
4. 素材 ID 自动得到真实 `content_id`，描述中的 Drama ID 正确。
5. GPU 成片只写 `/data/tt-post-publisher`，返回源/Logo/片尾/成片 SHA、大小、时长和裁剪秒数；旧 CTA 被裁掉，phone-match 过渡及音频连续。
6. 未选择隐私、未完成显式同意、描述缺少真实 Drama ID 时不能排期。
7. 重复素材、账号时间冲突、并发 claim、unknown 重发均被数据库/状态机阻止。
8. 三重门禁默认均为 0；验收过程中 TikTok Direct Post init 调用次数为 0。
9. `creator_info` 可在不泄露 Token 的前提下返回脱敏结果；失败时 fail-close。
10. CPU/GPU 服务、timer、隧道健康检查通过，且能按各自 immutable release 回滚。

## 风险与待确认

1. 当前内部发布池用途与 TikTok Direct Post Intended Use 冲突；没有合规产品形态前不得开启。
2. 当前片尾包含 DramaWave Logo 和推广引导，与 TikTok API Watermark Guidelines 冲突；真实 Direct Post 需合规替代素材或 TikTok 书面确认。
3. 需确认开发者 App 已获 Direct Post 审核、账号 Token 具备 `video.publish`、目标域名/路径已完成 URL Property 验证。
4. 新版片尾含示例 ID，动态 Drama ID 覆盖的视觉效果需在 GPU 实片中验收。
5. 公网成片存储和保留期需在开启真实发布前确认。

## 变更记录

- 2026-07-29：初版，纳入 CPU/GPU 分工、固定新版片尾和 TikTok 官方合规门禁。
- 2026-07-29：关闭态生产部署完成；真实 GPU/COS/账号预检与后台页面通过验收，Direct Post 未开启。
