# 007.x-post-canary 需求与技术设计

## 背景

为 AI 后台现有三个已授权 X 账号补齐“选取 Dramawave 昨日最高消耗合规素材并发布”的最小闭环。本次先执行一次真实灰度，仅用于确认 X 帖子正文、视频和短链跳转效果；不启用每日调度。

## 目标

- 从 `kunlunads_dev.ads_custom_source_insight` 读取 2026-07-22 的 Dramawave 消耗数据，按素材聚合后选择最高消耗候选。
- 对候选逐个执行违规记录、色情/暴力标签、素材可下载及视频可解码校验；任一信息不明确即跳过。
- 使用一个动态有效的 X 账号发布一条“视频 + 短链 + 剧描述”帖子。
- 在本地 SQLite 持久化队列与发布日志，并生成 `https://ai.yingliangads.com/s2l/<日志ID>.html` 短链跳转页。
- 返回可公开访问的 `https://x.com/<username>/status/<post_id>` 预览链接。

## 范围

### 包含

- 单条人工触发的真实灰度发布。
- 用户指定的 W2A 参数拼接规则和 URL 编码。
- X OAuth Access Token 到期时在账号锁内刷新并原子轮换 Token。
- X 媒体上传、处理状态轮询、Create Post、结果核验和脱敏日志。
- 幂等保护、失败可重试状态、部署备份及回滚记录。

### 不包含

- 每日 cron/systemd timer。
- 三账号批量发布或同文跨账号发布。
- 后台页面、人工审核 UI、自动改写剧描述。
- 对已有违规/标签数据做修正。

## 用户故事 / 业务规则

1. 数据日期使用 Asia/Shanghai 的前一天；本次固定为 `2026-07-22`。
2. 产品限定 Dramawave，消耗按素材聚合并从高到低尝试；`resource_id <= 0` 不得发布。
3. 候选在 Facebook、TikTok、Twitter 违规表或素材审核表中存在历史违规记录即排除；同时检查原素材 ID 映射。
4. 候选携带任何色情或暴力相关标签即排除，不按严重级别放宽；标签无法证明干净也排除。
5. 素材必须是可下载、非空、受支持的视频，并通过 `ffprobe`/等价预检。
6. 帖子正文固定为两行：`{url}\n{desc}`。描述为空或清洗后为空不得发布；超出 X 文本限制时只截断描述，不改 URL。
7. 长链基址固定为 `https://www.dramawavew2a.com/ads/101/2116/view`，参数名与业务规则一致并逐项 URL 编码。
8. `c` 参数固定为 `yingliang_post_CLV_VL_<用户名>*<Unix秒级时间戳>none<素材语言>*<剧名>*<标签>*<日志ID>`。
9. `af_dp` 使用 `content_id`；短链只能跳转到允许的 Dramawave W2A 主机和固定路径前缀。
10. 幂等键为 `账号ID + 数据日期 + 素材ID`；状态为 `publishing` 或 `published` 时拒绝再次发帖，避免网络超时后的盲重试。
11. X 上游调用必须在该账号的 sidecar 互斥锁内完成，Refresh Token 和 Access Token 不得离开 sidecar 进程或进入日志。

## 交互与流程

`只读选材 -> 合规门禁 -> 锁内刷新并校验账号 -> 创建 queued 队列/日志 -> 生成短链 -> 下载并 ffprobe -> 上传媒体 -> 创建 Post -> 更新 published 日志 -> 返回预览链接`

任何步骤失败均记录稳定错误码；Create Post 请求已发出但响应不确定时标记 `unknown`，禁止自动重试。

## 技术设计

### 影响模块

- `features/x_posts/`：URL、日志、短链、素材下载和 X API 客户端。
- `features/x_accounts/oauth_service.py`：内部灰度接口、账号锁内刷新及发布编排。
- `deploy/x-post-automation.service`：允许写入数据盘短链目录。
- `scripts/test_x_posts.py`、`scripts/test_x_accounts.py`：单元及接口契约回归。

### 数据结构

在现有 `accounts.sqlite3` 增量新增：

- `x_post_queue`：来源日期、账号、素材、剧、长短链、正文、状态、幂等键和时间戳。
- `x_post_publish_log`：队列 ID、尝试号、阶段、结果、X media/post ID、预览链接、脱敏错误和时间戳。

只使用 `CREATE TABLE/INDEX IF NOT EXISTS`，不修改或删除现有账号、OAuth state、OAuth event 数据。

### API / 接口

- `POST /internal/posts/canary`：仅 loopback + internal bearer；接收已经过数据库审计的单个候选和 `account_id`，执行一次发布。
- X 媒体接口：按官方 v2 分块上传流程执行 `INIT/APPEND/FINALIZE/STATUS`（以实现时核验的官方契约为准）。
- X 发帖接口：`POST https://api.x.com/2/tweets`，请求体包含文本和单个 `media_id`。

### 异常与边界

- 账号 disabled/revoked、scope 缺失、刷新失败：发布前失败，不调用 Create Post。
- 视频下载发生重定向到非 HTTPS、大小超限、类型不符或解码失败：发布前失败。
- 短链文件写入采用临时文件 + 原子替换；生成后必须公网 GET 并验证目标 URL。
- 媒体处理失败：记录 failed，可在人工确认后新建发布任务。
- Create Post 超时或响应不可解析：记录 unknown；只能先查证 X 账号后再人工决策。
- Create Post 成功但日志落库失败：响应保留 post ID，进入人工修复，不二次发布。

## 验收标准

- 数据库审计能证明所选素材是 2026-07-22 Dramawave 的最高消耗“可发布候选”，且违规和色情/暴力标签均为 0。
- 三个目标账号中选取的账号在发布时动态刷新成功、身份匹配且具备 `tweet.write`/`media.write`。
- 公网短链返回成功并最终到达指定长链，所有业务参数可还原且无空的关键 ID。
- X 返回一个 post ID；公开预览链接可打开，正文只有短链和剧描述，视频已附加。
- SQLite 中队列状态为 `published`，日志包含账号/素材/来源日期/长短链/post ID/时间且不含 Token。
- 部署 commit、备份路径、hash/权限、服务重启和回滚边界均有记录。

## 风险与待确认

- X 账号可能在发布瞬间被平台限制；按实时 API 返回失败关闭，不切换账号连续尝试。
- X 字符计数与普通字符串长度不同；实现需使用保守限制，避免到上游才失败。
- 本次真实发布已由用户明确授权；任何第二条发布仍需新的明确授权或后续已确认的自动化方案。

## 变更记录

- 2026-07-23：用户确认按已讨论默认方案执行一个账号、一条真实灰度 Post；不启用定时任务。
