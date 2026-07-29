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
