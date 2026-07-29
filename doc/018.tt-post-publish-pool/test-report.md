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
