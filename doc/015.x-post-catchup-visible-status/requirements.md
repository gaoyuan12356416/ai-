# 015.x-post-catchup-visible-status 需求与技术设计

## 背景

2026-07-27 每日批次在账号范围从 3 个扩至 9 个之前已经完成，因此账本中仅有账号 2、3、4 的 3 条已发布队列。管理员要求当天仅补发新增账号 5–10，并反馈 X 账号列表中没有直观看到“自动发布 Post”列。

线上核查确认列和 DTO 已存在，但表格最小宽度为 2080px，列位于第 5 位，1024/1100px 视口首屏不可见；HTML 响应也缺少缓存控制，旧标签页可能继续使用旧页面结构。

## 目标

1. 让“自动发布 Post”在常见桌面宽度首屏直接可见，并继续实时反映 Sidecar 当前解析的账号配置。
2. 在不改写原 3/3 日批次的前提下，为当天缺少队列的 6 个新增账号建立独立、可恢复、可审计的同日补发子批次。
3. 继续保持素材全局排重、账号/日期排重、整批预检、未知结果禁止盲重试及第二天自然 9 账号批次。

## 范围

### 包含

- 状态列移动至第 2 列，设置稳定最小宽度和不换行。
- 账号列表 HTML 禁止缓存，导航入口增加版本查询参数。
- 新增 `x_post_catchup_run` 和队列 `catchup_run_id`。
- 新增 daily-bearer-only 的补发查询、建批、失败审计接口。
- 新增显式参数的手工补发 runner；不接入 timer。
- 新增不带 Timer 的一次性 systemd oneshot，复用 daily 的账号、媒体修复、锁和系统权限边界。
- 以 2026-07-27、父批次 4、缺失数 6、原因 `scope_expansion_v1` 执行一次生产补发。

### 不包含

- 不修改父日批次的 `expected_count=3`、状态、计数或时间。
- 不伪造 2026-07-28 的运行日期。
- 不使用不支持素材池审计字段的 canary 接口。
- 不改变每日 10:00 定时器。

## 用户故事 / 业务规则

1. 补发目标必须等于当前有序配置账号减去当天所有既有队列账号，不能由调用方自由指定。
2. 父批次必须 `completed`，全部父队列及日志均已确认发布，且未知结果为 0。
3. 六个账号验证、六个 FIFO 素材的来源/合规/媒体/链接预检必须全部通过后，才能在一个事务中创建子批次、队列和素材绑定。
4. 队列必须且只能关联 daily run、catch-up run 二者之一；普通 canary 可两者都为空。
5. 429 或未知结果停止剩余补发；重入只能读取冻结的子批次，禁止重选素材。
6. 第二天正常日批次仍按新日期和 9 账号范围创建。
7. 本次一次性 runner 将无队列的 `failed_preflight` 也视作审计终态；
   不自动重新预检。若未来确需重试，必须新增独立的显式人工审批入口。

## 交互与流程

1. 操作员显式运行补发命令并提供日期、缺失数和固定原因。
2. runner 读取父日批次与既有补发子批次。
3. 若子批次存在，仅恢复其冻结队列；否则计算精确差集、验证账号、FIFO 选材并完成全量预检。
4. Sidecar 在事务点再次核验配置差集和父批次状态，再创建子批次。
5. 顺序发布六条队列，逐条落发布日志和预览链接。
6. 回读子批次、队列、日志、短链和 X 预览链接。

## 技术设计

### 影响模块

- `static/x-account-list.html`
- `static/navigation.json`
- `deploy/nginx-x-oauth.conf`
- `features/x_posts/service.py`
- `features/x_accounts/oauth_service.py`
- `features/x_accounts/client.py`
- `scripts/x_post_catchup_runner.py`
- `deploy/x-post-catchup.service`
- `deploy/x-post-catchup.env.example`
- X 相关测试、部署文档

### 数据结构

- `x_post_catchup_run`：以 `parent_run_id UNIQUE` 关联父日批次，保存日期、原因、账号范围、状态和计数。
- `x_post_queue.catchup_run_id`：可空；补发正式队列必须指向有效子批次。
- 触发器拒绝不存在的子批次、daily/catch-up 双父键以及日期不一致。

### API / 接口

- `POST /internal/posts/catchup-plan/query`
- `POST /internal/posts/catchup-plan`
- `POST /internal/posts/catchup-runs/record-failure`

以上仅允许 daily bearer 从 loopback 调用。

### 异常与边界

- 父批次未完成、有失败/未知结果、配置不是父账号的有序扩展、缺失数不匹配、素材不足、任一预检失败或唯一约束冲突时 fail closed，X 写入为 0。
- 建批响应丢失时视为可能已提交；重查子批次后才能继续。
- 发布请求进入未知状态后不得自动重试。

## 验收标准

- 1024px 视口下无需横向滚动即可看到状态列。
- 9 个有效账号显示“已配置”，停用账号显示“未配置”。
- 原父批次保持 3/3 完成且字段不变。
- 子批次精确包含账号 5–10 和 6 个不同 FIFO 素材。
- 六条均有唯一 queue/log/Post ID、短链和预览链接；无素材、账号日或 Post ID 重复。
- 下一次 timer 仍为 2026-07-28 10:00，未被手工补发改动。

## 风险与待确认

- X 上游出现限流或不确定响应时可能只完成部分补发；必须停止并人工核对。
- 补发会消耗 6 条素材，剩余库存需满足下一次 9 账号批次。

## 变更记录

- 2026-07-27：创建需求，确认采用独立 child catch-up batch。
