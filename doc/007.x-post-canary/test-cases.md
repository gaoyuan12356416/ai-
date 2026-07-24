# 测试用例

## 测试范围

队列/日志迁移、W2A URL、短链安全、下载预检、X 分块上传、Create Post、Token 刷新、互斥锁、幂等、内部鉴权和生产灰度验证。

## 测试数据

- 临时 SQLite 及虚拟账号/Token，所有 HTTP 使用 mock。
- HTTPS 素材 URL 的成功、重定向、超限、错误 MIME、损坏视频分支。
- 包含中文、空格、`*`、方括号的剧名/素材名/标签，用于 URL 编码回归。
- 生产仅使用通过只读审计的一条 2026-07-22 Dramawave 候选。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 增量建表 | 仅有旧 X 账号库 | 两次调用 `ensure_storage` | 两张新表/索引存在，旧表行与 Token 不变 | P0 | 本地、生产副本与生产均通过 |
| TC-002 | 长链编码 | 完整候选字段 | 构建 destination URL | 固定主机/路径，参数逐项正确，`af_dp=content_id` | P0 | 通过 |
| TC-003 | 非法目标拒绝 | 非 HTTPS/非白名单路径 | 生成短链 | fail closed，不落公开 HTML | P0 | 通过 |
| TC-004 | 短链原子生成 | queued 日志存在 | 写 HTML 两次 | 同一日志 ID 幂等，权限安全，目标无脚本注入 | P0 | 通过 |
| TC-005 | 帖子正文 | 剧名、长 URL 与长中文描述 | 生成文本 | 短链、引导语和剧名保持完整，描述保守截断且非空 | P0 | 通过 |
| TC-006 | 队列幂等 | 同账号/日期/素材重复创建 | 连续创建任务 | 返回同一任务；发布中/成功/未知禁止再次发帖 | P0 | 通过 |
| TC-007 | 视频下载边界 | mock 多类 HTTP 响应 | 执行预检 | 非 HTTPS、重定向、超限、错误类型和空文件均拒绝 | P0 | 通过 |
| TC-008 | 分块上传成功 | mock X INIT/APPEND/FINALIZE | 上传多分片视频 | segment 顺序正确；不记录 Authorization | P0 | 通过 |
| TC-009 | 媒体处理轮询 | FINALIZE 返回 pending | 轮询 STATUS | 成功后才发帖；failed/超时终止 | P0 | 通过 |
| TC-010 | Create Post 成功 | media ID 可用 | POST `/2/tweets` | 文本与 media ID 正确，保存 post ID/预览链接 | P0 | mock 与真实灰度均通过 |
| TC-011 | Create Post 结果不确定 | mock timeout | 执行发布 | 状态 unknown，禁止自动重试 | P0 | 通过 |
| TC-012 | 过期 Token 刷新 | active + refresh token | 进入发布上下文 | 锁内刷新、轮换 Token、身份一致后发布 | P0 | mock 与生产账号预检通过 |
| TC-013 | disabled/scope 缺失 | 不可发布账号 | 请求 canary | 在素材上传/Create Post 前拒绝 | P0 | 通过 |
| TC-014 | 发布/停用并发 | 发布持有账号锁 | 并发 logout | logout 等待；之后新发布被拒绝 | P0 | 通过 |
| TC-015 | 内部接口鉴权 | 非 loopback/错 bearer | POST canary | 403 且无数据库/网络副作用 | P0 | 通过 |
| TC-016 | 日志脱敏 | 上游错误含敏感字段 | 查询 DB/journal | Token、Authorization、素材鉴权查询串无命中 | P0 | 本地与生产均通过，敏感命中 0 |
| TC-017 | 真实候选合规门禁 | 生产只读数据 | 审计 top candidates | 所选素材所有违规和色情/暴力标签命中均为 0 | P0 | 通过：5221348 |
| TC-018 | 真实短链与 Post | 用户已授权灰度 | 发布一次并公网访问 | 短链/落地页/X status 均可访问，日志 published | P0 | 通过：log 1 / post 2080128600917905497 |

## 回归范围

- 原 X OAuth callback、个人/admin 查询、verify、soft logout 和 owner 隔离测试全量复跑。
- 现有主应用不改路由，不重启 `drama-material.service`。
- Nginx 既有 `/x-oauth/`、`/api/x-accounts`、静态后台页面保持不变。
