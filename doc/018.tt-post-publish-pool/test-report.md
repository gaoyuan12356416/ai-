# 测试报告

## 测试结论

本地自动化测试共执行 211 项，211 项全部通过：

- TT Post 新功能：111/111
- X 发布池回归：72/72
- 素材状态回归：28/28

Python 编译检查与 Git diff check 均通过。当前结果证明代码层面的核心状态机、CPU/GPU 协议、Runner 调度、页面契约和既有功能回归符合预期。

生产关闭态验收同时通过：真实 GPU NVENC 成片、COS 上传与公开访问、GPU 出口账号预检、AI 后台登录态页面均已验证。TikTok Direct Post 三重门禁全程关闭，未创建真实 Post。

## 测试范围

- TT Core：任务状态、参数冻结、幂等、时间转换、发布门禁和未知结果保护
- TT Service：账号与素材读取、预检、入队、取消、调和、GPU 协议和 Runner 行为
- GPU Worker：媒体准备、manifest、凭据封装、Direct Post 门禁、发布与调和协议
- TT UI：页面字段、账号选择、素材预览、发布时间、披露项、同意确认和状态展示
- App contract：路由、权限、内部服务调用和前端静态资源契约
- X 发布池回归：既有发布逻辑、存储层和 UI
- 素材状态回归：webhook 与广播逻辑

## 执行统计

| 测试集 | 通过 | 失败 | 阻塞 |
| --- | ---: | ---: | ---: |
| TT Core | 33 | 0 | 0 |
| TT Service | 32 | 0 | 0 |
| TT GPU | 25 | 0 | 0 |
| TT UI | 11 | 0 | 0 |
| TT App contract | 10 | 0 | 0 |
| **TT 小计** | **111** | **0** | **0** |
| X posts 回归 | 29 | 0 | 0 |
| X store 回归 | 34 | 0 | 0 |
| X UI 回归 | 9 | 0 | 0 |
| **X 小计** | **72** | **0** | **0** |
| 素材状态回归 | 28 | 0 | 0 |
| **总计** | **211** | **0** | **0** |

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

### 固定发布描述

- 页面描述框为只读，固定展示产品模板，仅 Drama ID 按当前素材动态替换。
- 客户端省略 `caption_text` 时，服务端仍会根据真实 `content_id` 生成完整描述。
- 即使保留正确 Drama ID，只要修改其他文案，服务端即返回 `tt_caption_fixed_template_mismatch`，且不会开始 GPU 制作。
- Core 冻结层再次要求唯一固定模板，数据库只保存固定模板及其真实 ID 渲染结果。

### 回归验证

- X 发布池 72 项既有测试全部通过，未发现 TT 新功能对 X 路由、存储或页面造成回归。
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
- P2：预览和正式入队可能重复转码，后续应增加预览产物复用机制。
- P2：SQLite 尚缺少显式 schema version 与迁移框架，后续升级前需补齐。
- Direct Post 平台审核、URL Property 验证以及品牌片尾的合规路径均未完成，因此不得开启真实发布。

## 生产关闭态验收

- 运行提交：`18148b2`
- CPU release：`/opt/tt-post/releases/18148b2`
- GPU release：`/opt/tt-post-gpu/releases/18148b2`
- CPU sidecar、每分钟 Runner timer、GPU sidecar 和 18830 反向隧道均为 active；SQLite `PRAGMA integrity_check=ok`。
- 只读快照返回 23 个候选账号；账号 `700` 从 GPU 实时确认：
  - `@dramawave998`
  - `Dramawave Short Dramas`
  - 隐私选项：`PUBLIC_TO_EVERYONE`、`MUTUAL_FOLLOW_FRIENDS`、`SELF_ONLY`
  - 最长视频：3600 秒
  - 账号/头像响应未包含账号 Token、Authorization 或带签名查询参数的头像 URL
- 实际素材 `5824343` 映射到 `Y9v1yQcFqM`，GPU NVENC 成片：
  - SHA-256：`568fde32b0bde91935a12af7bf732ffe537be99cc0e5fea94a1a2091d72ed492`
  - 大小：45,496,176 字节
  - 时长：45.685 秒
  - 1080 × 1920、30 fps、H.264 High、yuv420p、AAC-LC 48 kHz 双声道
  - 动态 Drama ID、教程标记和 0.9 秒 phone-match 过渡已抽帧检查
- COS `HEAD` 返回 200，`x-cos-meta-sha256` 与成片一致；`Range: bytes=0-1023` 返回 206、无重定向。
- GPU manifest `direct_post_eligible=false`、敏感标记数 0、publish ledger 文件数 0、残留 job 目录数 0。
- 公网页面 `/tt-post-pool.html` 返回 200 且 no-store；登录态浏览器显示 23 个账号，账号 700 和素材 5824343 的最终成片/描述预览正确，队列仍为 0。
- 既有 X sidecar、两个 X timer 和 GPU X 修复服务在部署后保持 active。

## 发布建议

当前仅允许继续运行三道 Direct Post 门禁关闭的准备/预检版本。完成 TikTok 平台审核、URL Property 验证与无品牌媒体合规确认之前，不得开启真实 Direct Post。
