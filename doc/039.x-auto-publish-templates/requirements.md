# 039.X Post 自动发布模板：需求与技术设计

## 背景

TT 推广平台已有独立的“自动发布模板”能力，可以按账号、语言、剧/素材指标规则和固定或随机时刻自动选材。X 推广平台当前只有人工维护的 Post 素材池、Post 短剧池及各自排期，没有同等级的模板化自动选材入口。

现有 X 发布链路已经承载生产账号授权、素材/剧集池排期、人工立即发布、全局素材去重、媒体修复、短链和发布账本。本需求必须在不改变这些既有链路默认行为的前提下增加新能力。

## 目标

- 在“X 平台推广”中新增“X Post 自动发布模板”和独立运行记录页面。
- 支持模板创建、编辑、复制、启用、停用、预览和确认后立即执行。
- 支持固定多个北京时间时刻或稳定随机 N 次/日。
- 按剧维度和素材维度分别配置 D0 ROAS、消耗、排序、语言、上线窗口、冷却和视频时长规则。
- 复用现有 X 账号资格、合规校验、媒体预检/修复、token 锁、全局素材去重、短链和最终发布账本。
- 新功能首次部署时模板为空且发布闸门关闭，不产生真实 X Post。

## 范围

### 包含

- 独立 `features/x_auto_posts/` 模块、loopback sidecar、SQLite、scheduler、runner、metric timer 和 systemd 单元。
- 独立模板、不可变版本、随机日计划、运行、账号任务、事件、素材冻结账本和完整日指标缓存。
- 模板选择一个明确的剧语言，并可选择多个当前可发布 X 账号。
- X 素材正文继续使用既有宏契约：`{{drama_name}}`、`{{desc}}` 必填且各一次，`{{url}}` 可选且最多一次。
- 自动模板任务通过既有 X sidecar 创建 `trigger_source=auto_template` 的隔离桥接批次，最终队列仍进入既有全局 `x_post_queue`，确保与素材池、剧集池、人工发布跨体系去重。
- 现有 `x_post_manual_run` 增量增加来源和可选冻结正文模板；人工入口仍固定使用 `trigger_source=manual`，仍读取现有素材池正文设置。
- 新自动 runner 与现有 X runner 共享发布进程锁；最终 X 请求继续在 `publish_credentials(...)` 账号锁内执行。

### 不包含

- 不修改现有 Post 素材池、Post 短剧池的选材、排期、模板或发布状态机语义。
- 不自动导入现有素材池、历史发布或 TT 自动模板。
- 不在首次部署时创建、启用或执行任何模板。
- 不通过真实 X Post 作为部署验收 canary。
- 不改变 X OAuth、软退出、账号所有权或 Premium 判定规则。

## 业务规则

### 模板与版本

1. 模板名称、账号、语言、正文模板、规则和排期必填；创建和复制后的模板默认停用。
2. 编辑生成不可变新版本；已经创建的运行继续使用原版本快照。
3. 启用只影响未来时刻；停用不取消已冻结的运行或已进入现有 X 队列的任务。
4. 同一账号可以属于多个已启用模板，但新系统按账号串行领取任务。
5. 立即执行必须二次确认并带稳定幂等键；API 只创建异步运行，不同步等待 X。
6. 所有写接口要求后台 Cookie、`x_accounts` 模块和 `xAutoPublishTemplates` 导航权限，并执行 same-origin 校验。

### 时间、筛选与指标

1. 业务日和计划按 `Asia/Shanghai`。
2. 固定模式允许多个不重复 `HH:mm`；随机模式生成当天稳定、不重复且不整点的计划，服务重启不重抽。
3. 指标窗口默认最近 7 个完整北京时间自然日，允许 1-30 天；窗口缺任何 READY 日时失败关闭。
4. 模板明确选择一个语言；剧要求 `app_id=1479`、已发布、`deploy_time` 不晚于当前时间且语言一致。
5. `resource_type_v2` 为可选多选，空数组表示不限。
6. 剧和素材分别使用包含式消耗/ROAS 范围、独立排序字段和方向；相同值使用稳定身份顺序。
7. 视频来源必须通过现有 X 严格来源、违规、标签、映射、完整下载和媒体校验。
8. 自动来源最长 600 秒/512 MiB；超过 140 秒只能路由给该账号当前 token 返回 `basic|premium|premium_plus` 的账号。资格不匹配只跳过该账号候选，不永久污染素材。
9. 预览不冻结。正式任务在最终选择前重读现有 X 素材占用状态，再在 X sidecar 中依靠 `x_post_queue.material_key` 唯一约束完成最终防重。
10. 新系统的素材冻结在既有 X queue 回读确认前是临时占用；只有获得唯一的 canonical queue ID 后才永久去重。确定性预检失败且 X run 已记录 `failed_preflight`、但尚无 queue/log/Post/unknown 证据时，临时占用必须原子释放；任何已确认队列和历史队列都不得删除或换号重建。

### 发布安全

1. 发布需要同时满足 `X_AUTO_POST_LIVE_ENABLED=1`、`X_AUTO_POST_ACCOUNT_AUDIT_APPROVED=1`、`X_AUTO_POST_URL_PROPERTY_VERIFIED=1`；默认均为 0。
2. sidecar 不持有 X Client Secret 或 token 文件；所有账号验证和最终 X 写入只经现有 X sidecar 内部接口。
3. `auto_template` 桥接批次不被现有 `x-post-manual.timer` 领取；只能由新 runner 处理。人工批次仍只领取 `manual`。
4. 任务一旦进入现有 X 队列，重试只能读取同一 queue/log；存在 Post ID、`unknown_outcome=1` 或 `post_creating` 时只允许对账，禁止重新发帖。
5. 现有人工、素材池、剧集池的默认查询、claim 和发布路径必须通过回归测试证明输出不变。
6. X sidecar 中断恢复必须按精确 auto run ID、来源和账号锁执行；锁忙只等待，锁空后以数据库 fence 终止遗留 queued/reserved/publishing 状态，绝不 claim 其他 run 或重新发帖。
7. 三道 live gate 关闭时禁止新选材、建计划和发布，但仍允许只读对账及精确终态恢复，避免自动 run 长期占用现有 X 账号。

## 交互与页面

1. 导航位于“X 平台推广”，顺序在账号列表之后、Post 素材池之前。
2. 列表页支持状态/名称筛选、创建、复制、启停、预览、立即执行和查看最近运行。
3. 编辑页包含模板名称、语言、账号、正文、统计窗口、剧规则、素材规则、冷却、固定/随机计划。
4. 运行页展示触发来源、模板版本、账号任务、冻结素材、桥接 X queue/log、错误和最终 Post URL；不返回媒体源 URL、token 或内部 bearer。

## 技术设计

### 独立边界

- 主 API 前缀：`/api/admin/x-auto-publish`，只做 Cookie/导航权限/审计和 loopback 代理。
- sidecar：`127.0.0.1:18833`，只暴露 loopback 管理/内部接口。
- SQLite：`/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3`。
- 新短期工作目录、锁和运行数据均在 `/mnt/data-disk/x-auto-post-publisher` 或 `/run/x-auto-post`。
- 新服务仅通过现有 `127.0.0.1:8810` X sidecar 内部 bearer 调用安全账号和队列接口。

### 数据结构

- `x_auto_template`、`x_auto_template_version`
- `x_auto_random_plan`
- `x_auto_run`、`x_auto_task`
- `x_auto_material_ledger`、`x_auto_event`
- `x_auto_metric_generation`、`x_auto_metric_daily`、active pointer
- 既有 X SQLite 增量字段：`x_post_manual_run.trigger_source`，默认 `manual`；不重建或删除旧表/索引。

## 验收标准

- 新页面/API/SQLite/scheduler/runner/metric 功能有针对性测试，且模板创建默认停用。
- 不登录为 401、无导航权限为 403、未知路由为 404；响应不包含敏感字段。
- 固定/随机计划、版本冻结、立即执行幂等、指标窗口、语言、两层排序、冷却、黑名单、X 全局去重和 Premium 路由覆盖测试。
- 现有 X 账号、素材池、剧集池、人工发布、排期、发布账本测试全部通过。
- 部署前在线备份现有 X SQLite、token 哈希/属主/模式、相关代码/静态页/单元；在副本上演练增量迁移并验证 `integrity_check=ok`。
- 部署验收仅使用健康检查、页面/API、离线预览、自然 `no_due/no_pending` 和队列/日志/Post 数量不变，不创建真实 X Post。

## 风险与待确认

- X 账号没有 TT 的“账号剧语言”配置，因此本设计使用模板级单语言；未来若需要账号级语言映射，新增版本字段，不回写旧模板。
- 新模板真正启用后会增加 X 发帖量；启用属于独立运营动作，不包含在首次部署。
- 现有 X sidecar 是生产关键路径，任何桥接改动必须保持人工与 schedule 默认分支字节级/行为级兼容，并仅做增量 schema。

## 变更记录

- 2026-08-11：根据 TT 自动发布模板和现有 X 发布边界形成初版设计。
