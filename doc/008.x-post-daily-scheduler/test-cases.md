# 测试用例

## 测试范围

增量迁移、批次/队列事务、全局素材排重、账号日排重、候选筛选、媒体预检、Sidecar 发布恢复、管理员日志接口/页面和 timer。

## 测试数据

- 临时 SQLite：旧 canary、重复素材、跨日/跨账号、reserved/publishing/published/unknown 状态。
- mock MySQL 候选：不同消耗、四类违规、危险标签、剧映射缺失/多义、无效 URL/媒体。
- mock Sidecar/X HTTP：成功、429、上传失败、Create Post unknown、进程恢复。
- 三个虚拟 active 账号，不使用真实 Token 或真实 Post。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 旧库增量迁移 | 007 schema + canary 记录 | 连续运行两次迁移 | 新表/列/索引存在，旧行和 Token 不变，旧素材已回填占用 | P0 | 通过 |
| TC-002 | 重复旧数据门禁 | 同素材两条 legacy queue | 运行迁移 | 明确失败，不静默建唯一索引 | P0 | 通过 |
| TC-003 | 全局素材排重 | 同素材不同账号/日期 | 两次事务入队 | 第二次唯一冲突，无第二队列 | P0 | 通过 |
| TC-004 | 账号日排重 | 同账号同 run_date 两素材 | 两次事务入队 | 第二次唯一冲突 | P0 | 通过 |
| TC-005 | 并发批次 | 两个 runner 同一天启动 | 同时 reserve run | 仅一个 run 和三条 queue | P0 | 通过（事务/唯一约束） |
| TC-006 | 候选稳定排序 | 同日多素材 | 按消耗筛选 | spend 降序、ID 升序，三素材不同 | P0 | 通过 |
| TC-007 | 四类违规排除 | 每表各命中一条 | 选择候选 | 所有命中素材排除 | P0 | 通过 |
| TC-008 | 危险标签排除 | 中英文色情/暴力标签 | 选择候选 | 素材/剧任一命中即排除 | P0 | 通过 |
| TC-009 | 剧映射 fail closed | 缺描述/标签或多义 | 选择候选 | 不进入三条计划 | P0 | 通过 |
| TC-010 | 媒体预检补位 | 高消耗 HEVC/坏媒体 | 预检候选池 | 跳过坏素材，继续直到凑齐三个 | P0 | 通过 |
| TC-011 | 成组入队 | 仅找到两条合格素材 | 创建计划 | 仅记录 `failed_preflight` run，不创建正式 queue/Post | P0 | 通过 |
| TC-012 | reserved 恢复 | 已有 reserved queue | 重新运行 | 同一 queue 恢复，不新建 | P0 | 通过 |
| TC-013 | X 写阶段防重 | media_uploading/post_creating | 重复运行 | 不发生第二次 Create Post | P0 | 通过 |
| TC-014 | unknown 停批 | 第一/第二账号 Create Post unknown | 运行批次 | 标记待确认并停止剩余发布 | P0 | 通过 |
| TC-015 | 429 停批 | X 返回 429 | 运行批次 | 当前失败并停止剩余账号 | P0 | 通过 |
| TC-016 | 管理员日志接口 | Cookie admin | 分页/筛选查询 | 仅安全字段、no-store、上限 100 | P0 | 通过 |
| TC-017 | 鉴权隔离 | 普通用户/API Token | 查询日志 | 403，不返回数据 | P0 | 通过 |
| TC-018 | 日志页面安全 | 恶意素材名/错误文本 | 页面渲染 | 转义成功，外链 allowlist，DOM 无敏感值 | P0 | 通过 |
| TC-019 | timer 首日门禁 | 部署日早于 start_date | Persistent 补跑 | 只记录 skip，不创建 queue/Post | P0 | 通过（离线），生产待复核 |
| TC-020 | timer 下一次触发 | 已启用 timer | `systemctl list-timers` | 下一次为北京时间次日 10:00 | P0 | 待执行 |
| TC-021 | 三账号真实首轮 | 首个正式 run 到点 | 只读审计 + 日志核对 | 三账号各一条、三素材不同、日志完整 | P0 | 待自然触发验收 |
| TC-022 | 合规证据缺失/冲突 | 五项证据缺失、NULL 或别名不一致 | 创建 daily plan | 整批拒绝且无 queue | P0 | 通过 |
| TC-023 | 存储 fail closed | 数据盘未挂载/不可写/空间不足 | storage preflight | 账号、选材、计划和 Post 均不执行 | P0 | 通过（离线），生产待复核 |
| TC-024 | 媒体内容变化 | 预检后同 URL 返回不同内容 | 正式下载 | SHA-256/尺寸不一致，X 请求数为 0 | P0 | 通过 |
| TC-025 | ffprobe 密钥隔离 | 服务环境含 X/MySQL 密钥 | 调用 probe | 子进程仅收到 LANG/LC_ALL/PATH | P0 | 通过 |
| TC-026 | 发布前失败占坑 | 短链写失败或账号用户名已变化 | publish-by-queue | 日志从 `reserved` 原子转 known failure，不调用 X | P0 | 通过 |
| TC-027 | Create Post 响应截断 | 请求已写出但响应 `IncompleteRead` | 执行当前账号 | 日志/run 进入 unknown/needs_review，停止余下账号 | P0 | 通过 |
| TC-028 | 成功响应结构异常 | Sidecar HTTP 200 返回空对象或链接/ID 不一致 | runner 解析发布结果 | 按 unknown 停批，不默认 `published` | P0 | 通过 |
| TC-029 | 候选预检磁盘高水位 | 多个连续坏媒体候选 | 下载/ffprobe/补位 | 每次候选结束立即删除，临时文件峰值为一份 | P0 | 通过 |
| TC-030 | 挂载 point-of-use | 初检后数据盘消失或设备变化 | 建计划/写短链/X 写入 | 再次 fail closed，生产固定路径不在根盘自动创建 | P0 | 通过（离线），生产待复核 |
| TC-031 | Sidecar 响应读取失败 | publish 请求已发出，响应截断/超大/畸形 | runner 读取响应 | 一律 unknown 并停止后续账号 | P0 | 通过 |
| TC-032 | Post 已创建但落库失败 | X 已返回明确 Post ID，最终 SQLite commit 异常 | 完成发布 | 保留 Post ID/URL，日志 unknown、run needs_review，禁止重发 | P0 | 通过 |
| TC-033 | 新部署目录门禁 | `media-work` 缺失或不在数据盘 | storage preflight | 计划前失败；部署必须显式创建正确 owner/mode | P0 | 通过（离线），生产待复核 |
| TC-034 | Sidecar 错误体截断 | publish 已发出，HTTP error body `IncompleteRead` | runner 读取 | unknown 停批，不让原始异常逃逸 | P0 | 通过 |
| TC-035 | 素材流截断补位 | 高消耗候选下载中断 | preflight 候选池 | 当前候选 known reject，立即清理并选择下一素材 | P0 | 通过 |
| TC-036 | 媒体处理 known taxonomy | X 媒体失败/超时/响应过大且尚未 Create Post | runner 解析 | 明确 known failure，按策略继续其余账号 | P0 | 通过 |
| TC-037 | 危险词派生形态 | 标签为 weapons/nudes/murders/suicidal | selector | 全部 fail closed；端到端不入选 | P0 | 通过 |
| TC-038 | daily-plan 非 ASCII 上限 | 三条各 2000 个中文字符描述 | Sidecar route | 超过普通 16KiB 仍接收；超过独立 256KiB 硬上限返回 413 | P0 | 通过 |
| TC-039 | publish 错误 outcome | 畸形/未知 409 或 marker 缺失 | runner 解析 publish | 一律 unknown 停批；显式 known marker 才可继续 | P0 | 通过 |
| TC-040 | daily token 路由隔离 | 使用专用 daily bearer | 调 canary/authorize/accounts/logs/runs | 全部 403；不能回退复用 backend token | P0 | 通过 |
| TC-041 | 固定三账号/正式队列 | daily bearer 提交越界账号或 legacy queue | daily-plan/publish | 越界 403；仅固定三账号且带 run_id 的 queue 可执行 | P0 | 通过 |
| TC-042 | 短链掉电持久化 | 新建/同内容重放短链 | 故障注入 chmod/fsync/replace | fchmod→文件 fsync→replace→目录 fsync；失败发生在 Create Post 前 | P0 | 通过 |
| TC-043 | plan 响应身份 | 重复 queue/account ID、错误 run ID/日期/素材 | runner 解析计划 | plan unknown，禁止进入 publish | P0 | 通过 |
| TC-044 | Create Post 非 JSON 5xx | 500/503 HTML 或截断 JSON | Create Post | unknown/needs_review，禁止重发 | P0 | 通过 |
| TC-045 | plan 明确回滚 | Sidecar 409 + outcome_known | runner 处理 | 记录 failed_preflight；通道丢失仍不覆盖可能已提交 run | P0 | 通过 |
| TC-046 | X 限流统一停止 | HTTP 429、usage-capped、rate-limit-exceeded、code 88 | verify/upload/Create Post | 稳定 `x_post_rate_limited`/429，首条后停止，run=stopped | P0 | 通过 |
| TC-047 | root-only env 启动 | systemd 注入 env，进程用户不可读 0600 文件 | Sidecar 启动 | EACCES 安全跳过重读，配置门禁仍生效 | P0 | 通过（离线），生产待复核 |
| TC-048 | loopback 禁代理 | 环境设置恶意 http_proxy/https_proxy | backend/runner/health 访问 Sidecar | 仍直连 loopback，bearer 不发往代理 | P0 | 通过 |
| TC-049 | 常见色情暴力 taxonomy | gun/shooting/torture/knife/R18/18+ 与枪支/涉黄/殴打/流血等 | selector | 全部 fail closed，安全词不误拦 | P0 | 通过 |

## 回归范围

- 007 canary 的 55 项测试全量复跑。
- X 授权、verify、soft logout、owner/admin 隔离和公开 callback/health 保持不变。
- 主 AI 后台其他页面/API 不回退；Nginx 静态与 quick-nav 保持可用。
