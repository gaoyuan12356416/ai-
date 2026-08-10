# 037.x-post-premium-long-video 需求与技术设计

## 背景

生产 X 自动发布在媒体预检和 GPU 修复层统一按 `0.5-140s` 处理；超过 140 秒的素材会被拒绝或裁到 139 秒。X 账号快照目前只保存 `verified`，没有通过个人账号 OAuth token 保存会员类型，因此无法把长视频安全地路由到具备长视频权益的账号。

## 目标

1. 使用每个个人 X 账号自己的 OAuth access token 调用 `GET /2/users/me?user.fields=subscription_type,...`，保存当前会员类型。
2. 素材池允许保留并参与筛选超过 140 秒的视频。
3. 原始时长超过 140 秒的视频只分配、上传和发布给 X 明确返回 `Basic`、`Premium` 或 `PremiumPlus` 的账号。
4. 保持短视频、全局去重、账号发布审批、随机/固定排期和失败/未知结果语义不变。

## 范围

### 包含

- X OAuth 账号快照的加法字段、主动校验和列表 DTO。
- 素材池正式调度的账号感知媒体预检与确定性分配。
- 长视频媒体上传类别、发布前二次媒体校验和失败关闭。
- GPU 媒体修复的标准/会员时长策略，避免会员长视频被裁到 139 秒。
- 管理员账号页、素材池页提示、接口文档、测试和生产部署/回滚。

### 不包含

- 不购买、续费或变更任何 X Premium 订阅。
- 不根据蓝 V、`verified`、粉丝数或用户名推断会员资格。
- 不为验收创建真实 X Post，也不手动触发正式调度。
- 不承诺网页端 3/4 小时上限可通过当前 X API 自动发布。
- 不改变短剧池既有账号亲和性；所有发布入口仍执行最终长视频会员门禁。

## 用户故事 / 业务规则

1. `subscription_type` 只信任已认证用户自己的 `/2/users/me` 响应。
2. 原始值标准化为 `none|basic|premium|premium_plus|unknown`；仅后三者具备长视频权益。
3. 字段缺失、未知新枚举、请求失败、身份不匹配或快照未同步时，长视频资格必须为 false。
4. 每次正式发布前沿用现有 `verify_account` 实时刷新 token/身份/会员快照，再在账号锁内发布。
5. 普通账号媒体时长为 `0.5-140s`；会员账号自动发布时长为 `0.5-600s`，文件仍不超过 512 MiB。
6. `<=140s` 使用 `tweet_video`；`>140s` 使用 `amplify_video`。
7. 素材池按既有 `created_at DESC,id DESC` 扫描。短素材优先填充非会员空位；检测到长素材后只匹配仍未分配的会员账号，最终队列仍按冻结账号顺序提交。
8. 没有可用会员账号时，长素材保持未绑定、未发布，记录非阻塞原因 `x_long_video_requires_premium`，后续会员状态变化后可重新预检。
9. 会员在计划冻结后失效时，发布前实时校验必须阻止媒体上传；队列按现有明确失败语义落账，不得换号或自动重试。

## 交互与流程

`账号授权/同步 -> token 调用 /2/users/me -> 保存 subscription_type -> 到点刷新目标账号 -> 素材下载/探测 -> 按时长匹配会员账号 -> 原子冻结队列 -> 发布前再次刷新账号 -> 按时长选择 media_category -> 顺序发布`

## 技术设计

### 影响模块

- `features/x_accounts/oauth_service.py`
- `features/x_posts/service.py`
- `features/x_posts/selector.py`
- `features/x_posts/media_repair.py`
- `scripts/x_post_daily_runner.py`、`scripts/x_post_schedule_runner.py`
- `static/x-account-list.html`、`static/x-accounts.html`、`static/x-post-material-pool.html`
- X 账号、发布、排期、GPU 修复相关测试和部署示例。

### 数据结构

- `x_authorized_account.subscription_type TEXT NOT NULL DEFAULT 'unknown'`。
- `x_post_queue.preflight_duration REAL NOT NULL DEFAULT 0`，保存计划冻结前的秒数审计值。
- 迁移必须加法、幂等；历史账号默认为 `unknown`，历史队列默认为 `0`。

### API / 接口

- X 账号 DTO 新增 `subscription_type`、`premium_subscriber`、`long_video_eligible`、`long_video_publish_eligible`。
- 排期账号选项透传安全会员字段，仅用于展示和服务端候选路由，不替代 `publish_eligible`。
- CPU -> GPU 修复请求新增严格枚举 `duration_policy=standard|premium`，并升级固定修复 profile。

### 异常与边界

- 140.000 秒仍按短视频；大于 140.000 秒才需要会员。
- 600.000 秒允许会员发布；大于 600 秒返回 `invalid_media_duration`，必要时仅按会员策略裁到 599 秒的修复路径处理。
- `Basic` 也包含长视频上传权益；蓝 V 不作为判断依据。
- 已冻结/已发布/失败/未知队列的去重和不可复用边界不变。
- X API 的自动上传上限与网页/iOS 产品上限不同；本功能只承诺当前自动化合同的 600 秒。

## 验收标准

1. 完整、缺失、`None`、`Basic`、`Premium`、`PremiumPlus` 和未知枚举均有边界测试。
2. 混合账号批次中，长素材不会分配给非会员；短素材仍可分配给任意已批准账号。
3. 非会员长素材不再触发 139 秒裁剪；会员长素材在需要编码修复时保持原时长（不超过 600 秒）。
4. 发布层对真实下载媒体再次探测并选择正确 `media_category`。
5. 数据库迁移演练通过，账号/token 非敏感 hash/mode 不变。
6. 本地聚焦测试、全量 X 测试、语法检查、diff check 通过。
7. 生产 CPU/GPU 精确提交部署、窄重启、健康检查、会员同步和页面/API 验证通过；不创建测试 Post，队列/日志计数无意外增加。

## 风险与待确认

- X 官方网页会员长视频上限高于 X API 文档的普通 `tweet_video` 合同；本实现对 `>140s` 使用官方 chunked-upload 指南支持的 `amplify_video`，并保守限制为 600 秒。首次自然长视频仍是最终平台兼容性证据。
- `subscription_type` 是动态权益；任何未知/缺失都失败关闭，可能导致长素材暂时跳过。
- GPU 长视频转码耗时和输出大小明显增加，继续受单 GPU 槽、512 MiB 输出上限和现有批次预检约束。

## 变更记录

- 2026-08-10：根据用户需求建立设计；确认生产基线 `0d36c7b56b8b415a1ab5776249540c5a7c0e8fb6`，正式排期账号为 5 个，次日随机计划已冻结。
- 2026-08-10：实现时确认素材详情 SQL 仍有 `video_duration<=140` 且选择器会重排为最旧优先；已统一为 `<=600` 和 `created_at DESC,id DESC`，否则长素材无法真正进入账号感知预检。
