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

本地执行以下六个 TT 相关测试集，共 `189/189` 通过，用时 12.772 秒：

| 测试集 | 通过 | 失败 |
| --- | ---: | ---: |
| TT Core | 49 | 0 |
| TT Service + Runner | 70 | 0 |
| TT GPU | 25 | 0 |
| TT 发布池 UI | 23 | 0 |
| TT 个号设置 UI | 11 | 0 |
| TT App contract | 11 | 0 |
| **合计** | **189** | **0** |

执行命令：

```text
python -m unittest scripts.test_tt_posts_core scripts.test_tt_posts_service scripts.test_tt_gpu_worker scripts.test_tt_post_pool_ui scripts.test_tt_account_settings_ui scripts.test_tt_posts_app_contract
```

### 上线前复审发现并关闭

| 级别 | 复审发现 | 本地关闭方案与证据 | 状态 |
| --- | --- | --- | --- |
| P0 | 同一 run 并发执行时，一个执行者预检报错释放素材，另一个执行者仍可能冻结 queue，形成错误释放或孤儿 queue | 增加每个 run 独占的 120 秒 execution lease 与不可外泄的 fencing token；`freeze/release/bind` 均在事务内核验 run、pool、lease 与 token 身份；本地覆盖 release-first、freeze-first、lease 到期接管及过期 owner 拒绝 | 本地关闭 |
| P1 | 从已配置账号切到未配置/加载中账号时，时间控件可能沿用上一账号时间 | 未配置或加载态先重置为默认 `11:00`，再按当前账号数据渲染；本地页面合同测试通过 | 本地关闭 |
| P1 | 主应用公共兼容 `POST /queue` 可绕过新入口，在门禁关闭时 reserve 素材 | 主应用精确 `/api/admin/tt-posts/queue` 方法白名单改为仅 `GET`；保留 GET 查询及动态 cancel/reconcile，移除公共兼容写入转发 | 本地关闭 |

### 已由本地自动化证明的增量事实

- 以素材 4665764 的 2087 秒属性构造的本地 fixture 可通过 TT `1..3600` 秒 resolver 合同；X selector 隔离合同另有本地回归固定 SQL 参数为 `1,140`，但不计入本次 TT `189/189`。
- 每日设置与待发素材分开持久化；同一自然日时点幂等，账号级 FIFO 和账号隔离成立。
- 发布宽限固定为 600 秒；非 600 配置被拒绝，宽限内恢复与超窗 `missed` 均有自动化覆盖。
- `claim → freeze` 中断后可在后续 minute tick 找回 claimed run；`freeze → bind` 中断后按稳定 queue 幂等键找回既有 queue。两类恢复均未创建重复 run/queue 或重复消费素材。
- 同一 run 的执行权由 120 秒独占 lease 和 fencing token 约束；过期 owner 不能在新 owner 释放后继续冻结，也不能在 queue 已冻结后错误释放素材。
- 切换到未配置或加载中的账号时，发布时间恢复默认 `11:00`，不会继承上一账号的值。
- 主应用公共精确 `/queue` 路由只读；门禁关闭时不能通过兼容 POST reserve 素材，GET/cancel/reconcile 能力保持。
- 手动请求 key 按账号保存到固定、非敏感的 `sessionStorage` 映射；读取时校验账号格式、key 前缀/长度/字符、状态、总长度和账号数量。成功或明确未发布删除对应账号，`unknown` 或未确认结果保留。
- Runner 先处理既有到期 queue，再执行 recurring due；daily due 新建 queue 后只使用剩余 `claim_limit`，不会把单 tick 领取上限翻倍。
- 三项门禁任一关闭时，手动发布在领取素材前 fail-close；本地断言素材仍为 `available`、queue 为空且 publish 调用为 0。

### 尚未完成的生产验收

| 验收项 | 结果 |
| --- | --- |
| Git 提交、远端分支和不可变 release 对应关系 | 待填写 |
| CPU/GPU 备份、部署、服务重启与回滚演练 | 待填写 |
| 生产 SQLite 七表增量迁移、完整性和真实行数 | 待填写 |
| 生产 timer/path 唤醒、600 秒宽限及 claim 总预算 | 待填写 |
| 三项门禁关闭时 pool/run/queue 不消费证据 | 待填写 |
| 素材 4665764 真实 preview、成片时长和目标账号 3600 秒能力 | 待填写 |
| 登录态页面的每日排期、FIFO 数量、跨刷新/多账号手动幂等 | 待填写 |
| 公网 no-store、静态资源 hash 和 TikTok 外部请求计数 | 待填写 |

在上述项目填写并复核前，本节不得作为“已生产部署”或“线上通过”的依据。
