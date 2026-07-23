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
| TC-001 | 旧库增量迁移 | 007 schema + canary 记录 | 连续运行两次迁移 | 新表/列/索引存在，旧行和 Token 不变，旧素材已回填占用 | P0 | 待执行 |
| TC-002 | 重复旧数据门禁 | 同素材两条 legacy queue | 运行迁移 | 明确失败，不静默建唯一索引 | P0 | 待执行 |
| TC-003 | 全局素材排重 | 同素材不同账号/日期 | 两次事务入队 | 第二次唯一冲突，无第二队列 | P0 | 待执行 |
| TC-004 | 账号日排重 | 同账号同 run_date 两素材 | 两次事务入队 | 第二次唯一冲突 | P0 | 待执行 |
| TC-005 | 并发批次 | 两个 runner 同一天启动 | 同时 reserve run | 仅一个 run 和三条 queue | P0 | 待执行 |
| TC-006 | 候选稳定排序 | 同日多素材 | 按消耗筛选 | spend 降序、ID 升序，三素材不同 | P0 | 待执行 |
| TC-007 | 四类违规排除 | 每表各命中一条 | 选择候选 | 所有命中素材排除 | P0 | 待执行 |
| TC-008 | 危险标签排除 | 中英文色情/暴力标签 | 选择候选 | 素材/剧任一命中即排除 | P0 | 待执行 |
| TC-009 | 剧映射 fail closed | 缺描述/标签或多义 | 选择候选 | 不进入三条计划 | P0 | 待执行 |
| TC-010 | 媒体预检补位 | 高消耗 HEVC/坏媒体 | 预检候选池 | 跳过坏素材，继续直到凑齐三个 | P0 | 待执行 |
| TC-011 | 成组入队 | 仅找到两条合格素材 | 创建计划 | 不创建正式 queue/Post | P0 | 待执行 |
| TC-012 | reserved 恢复 | 已有 reserved queue | 重新运行 | 同一 queue 恢复，不新建 | P0 | 待执行 |
| TC-013 | X 写阶段防重 | media_uploading/post_creating | 重复运行 | 不发生第二次 Create Post | P0 | 待执行 |
| TC-014 | unknown 停批 | 第一/第二账号 Create Post unknown | 运行批次 | 标记待确认并停止剩余发布 | P0 | 待执行 |
| TC-015 | 429 停批 | X 返回 429 | 运行批次 | 当前失败并停止剩余账号 | P0 | 待执行 |
| TC-016 | 管理员日志接口 | Cookie admin | 分页/筛选查询 | 仅安全字段、no-store、上限 100 | P0 | 待执行 |
| TC-017 | 鉴权隔离 | 普通用户/API Token | 查询日志 | 403，不返回数据 | P0 | 待执行 |
| TC-018 | 日志页面安全 | 恶意素材名/错误文本 | 页面渲染 | 转义成功，外链 allowlist，DOM 无敏感值 | P0 | 待执行 |
| TC-019 | timer 首日门禁 | 部署日早于 start_date | Persistent 补跑 | 只记录 skip，不创建 queue/Post | P0 | 待执行 |
| TC-020 | timer 下一次触发 | 已启用 timer | `systemctl list-timers` | 下一次为北京时间次日 10:00 | P0 | 待执行 |
| TC-021 | 三账号真实首轮 | 首个正式 run 到点 | 只读审计 + 日志核对 | 三账号各一条、三素材不同、日志完整 | P0 | 待自然触发验收 |

## 回归范围

- 007 canary 的 55 项测试全量复跑。
- X 授权、verify、soft logout、owner/admin 隔离和公开 callback/health 保持不变。
- 主 AI 后台其他页面/API 不回退；Nginx 静态与 quick-nav 保持可用。
