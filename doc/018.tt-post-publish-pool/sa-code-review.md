# SA 代码评审

## 评审结论

当前实现未发现阻断合入的 P0 问题。TT Post 发布池的 CPU/GPU 边界、Token 传递、幂等与未知结果保护、定时 Runner 调度顺序以及 Direct Post 关闭态均已按设计落地，并通过自动化测试与生产关闭态验收。

CPU 当前以提交 `2fd07d3e1a6a7ef13982263cbf44297ef4a94156`
运行于 `/opt/tt-post/releases/2fd07d3`；GPU 保持
`/opt/tt-post-gpu/releases/18148b2`。真实 GPU NVENC、COS 上传、账号
`creator_info` 与浏览器页面已经验证；TikTok Direct Post 三项门禁始终关闭，发布池任务总数为 0，未调用 TikTok 发布初始化、未创建或发布真实帖子。

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

### 4. 固定发布描述

- 固定模板由 CPU Core 定义为唯一真值，页面只读展示，只有素材真实 `content_id` 可动态替换。
- Sidecar 对客户端提交值做全文逐字校验；保留正确 Drama ID 但修改其他文案也会被拒绝。
- Core 冻结层再次拒绝非固定模板，防止内部调用绕过页面和 Sidecar 校验。
- GPU 发布协议和描述指纹无需改变，继续使用数据库中已冻结的最终描述。
- 幂等查询先识别既有任务，再对新建任务执行固定模板校验；历史自定义描述可原样重放，但不能用于新建任务。

## 问题与遗留风险

| 编号 | 级别 | 问题 | 当前控制 | 后续建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | GPU 服务目前仍以 root 运行，且为访问 NVENC 使用 `PrivateDevices=false` | systemd 已启用 `ProtectHome`、只读路径约束，并通过 `InaccessiblePaths` 阻断已知 SSH、业务 `.env` 等秘密路径 | 建立专用 GPU 服务用户和独立 Python 运行环境，完成最小权限迁移后再重新评审 | 遗留 |
| CR-002 | P2 | 预览与正式入队可能对同一素材重复转码 | 使用稳定预览键、任务键和内容寻址产物降低重复概率 | 后续将预览产物提升为可复用的正式任务产物，或增加显式复用协议 | 待优化 |
| CR-003 | P2 | SQLite 表结构尚无显式 schema migration/version 管理 | 当前为首版新建库，建表逻辑保持幂等 | 增加 schema version 表和逐版本迁移脚本，并纳入升级/回滚测试 | 待优化 |

## 已关闭问题

| 编号 | 原级别 | 问题 | 处理结果 |
| --- | --- | --- | --- |
| CR-CLOSED-001 | P1 | reconcile 先执行可能导致到期任务错过发布窗口 | 调整为 claim/publish 优先，reconcile 每轮最多 5 条 |
| CR-CLOSED-002 | P1 | 单条 publish 异常可能中断整个 Runner 批次 | 增加单条异常隔离并继续处理后续任务 |
| CR-CLOSED-003 | P1 | claim lease 与发布宽限窗口关系不安全 | lease 固定为 300 秒，小于 600 秒宽限期 |

## 编译与验证结果

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

## 合入建议

当前关闭门禁版本可继续用于账号预检、成片、预览和发布池排期演练。生产启用 Direct Post 前，仍必须分别完成平台审核、URL Property 验证、无品牌媒体策略确认和 TikTok 状态调和链路验收。
