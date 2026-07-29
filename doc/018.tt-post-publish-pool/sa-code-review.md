# SA 代码评审

## 评审结论

上一生产基线未发现阻断合入的 P0 问题。TT Post 发布池的 CPU/GPU 边界、Token 传递、幂等与未知结果保护、定时 Runner 调度顺序以及 Direct Post 关闭态均已按初版设计落地，并通过当时的自动化测试与生产关闭态验收。

CPU 当前以提交 `2fd07d3e1a6a7ef13982263cbf44297ef4a94156`
运行于 `/opt/tt-post/releases/2fd07d3`；GPU 保持
`/opt/tt-post-gpu/releases/18148b2`。真实 GPU NVENC、COS 上传、账号
`creator_info` 与浏览器页面已经验证；TikTok Direct Post 三项门禁始终关闭，发布池任务总数为 0，未调用 TikTok 发布初始化、未创建或发布真实帖子。

2026-07-29 批量素材与可编辑描述属于尚未部署的增量改动。本轮设计采用现有单项 preview/queue 路由、浏览器顺序编排和逐项失败隔离，不新增数据库表。当前评审结论为“待自动化与浏览器验证”；不得把本文件中的旧生产版本、旧测试计数解释为新改动已经上线。

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

### 4. 上一生产基线的固定发布描述（历史）

- 固定模板由 CPU Core 定义为唯一真值，页面只读展示，只有素材真实 `content_id` 可动态替换。
- Sidecar 对客户端提交值做全文逐字校验；保留正确 Drama ID 但修改其他文案也会被拒绝。
- Core 冻结层再次拒绝非固定模板，防止内部调用绕过页面和 Sidecar 校验。
- GPU 发布协议和描述指纹无需改变，继续使用数据库中已冻结的最终描述。
- 幂等查询先识别既有任务，再对新建任务执行固定模板校验；历史自定义描述可原样重放，但不能用于新建任务。

以上五点只描述当前生产提交 `2fd07d3`，将被下列增量合同替代，但历史任务本身不得改写。

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
| CR-002 | P2 | 预览与正式入队可能对同一素材重复转码 | 使用稳定预览键、任务键和内容寻址产物降低重复概率 | 后续将预览产物提升为可复用的正式任务产物，或增加显式复用协议 | 待优化 |
| CR-003 | P2 | SQLite 表结构尚无显式 schema migration/version 管理 | 当前为首版新建库，建表逻辑保持幂等 | 增加 schema version 表和逐版本迁移脚本，并纳入升级/回滚测试 | 待优化 |
| CR-004 | P2 | 批量状态仅存在浏览器，不具备服务端批次恢复 | 每条 queue 使用独立稳定幂等键；已成功任务是数据库事实，刷新后可从队列查询 | 若后续需要暂停/恢复整批，再单独设计批次账本；本轮不得隐式增加 | 已接受 |
| CR-005 | P2 | 100 个素材顺序预览可能耗时较长 | 页面显示当前项/总数并逐项失败隔离；确定性 prepare 支持重复请求复用 | 生产浏览器验收实际响应时间，再决定是否引入异步批次能力 | 待验证 |

## 已关闭问题

| 编号 | 原级别 | 问题 | 处理结果 |
| --- | --- | --- | --- |
| CR-CLOSED-001 | P1 | reconcile 先执行可能导致到期任务错过发布窗口 | 调整为 claim/publish 优先，reconcile 每轮最多 5 条 |
| CR-CLOSED-002 | P1 | 单条 publish 异常可能中断整个 Runner 批次 | 增加单条异常隔离并继续处理后续任务 |
| CR-CLOSED-003 | P1 | claim lease 与发布宽限窗口关系不安全 | lease 固定为 300 秒，小于 600 秒宽限期 |

## 上一生产基线的编译与验证结果

- TT 测试：112/112 通过
  - Core：33
  - Service：33
  - GPU：25
  - UI：11
  - App contract：10
- X 回归：72/72 通过
  - X posts：29
  - Store：34
  - UI：9
- 素材状态回归：28/28 通过
- Python 编译检查：通过
- Git diff whitespace/check：通过

上述 `212/212` 和浏览器结果对应当前生产固定描述版本，不覆盖本轮未部署增量。增量合入前至少需要重新执行 TT Core、Service、GPU、UI、App contract、X 回归和素材状态回归，并将实际计数写入 `test-report.md`。

## 合入建议

当前生产关闭门禁版本可继续用于账号预检、成片、预览和发布池排期演练。本轮批量/可编辑描述改动在自动化、代码复审、CPU immutable release 和登录态浏览器验收完成前不建议部署。部署后仍必须保持三项 Direct Post 门禁关闭；生产启用 Direct Post 前，继续要求平台审核、URL Property 验证、无品牌媒体策略确认和 TikTok 状态调和链路验收。
