# SA 评审意见

## 结论

有条件通过。架构和状态机可开发，但真实 Direct Post 必须默认关闭；审核、Intended Use、URL Property 和品牌片尾问题未解决前不得以运维方式绕开。

2026-07-29 增量评审结论：批量素材和可编辑描述方案有条件通过。采用“浏览器逐条调用既有 preview/queue”的兼容路径，不新增批量 API 或数据库表；部分成功是明确业务语义。代码必须通过本文件新增的幂等、时间序列、UTF-16 和历史重放门槛后才可部署关闭态版本。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | 合规 | 内部账号上传工具被 TikTok 官方列为不可接受用途 | 三重门禁默认关闭，只交付关闭态能力 | 已采纳 |
| SA-002 | P0 | 素材 | 新版片尾含 Logo、品牌和推广引导 | 真实 API 发布前提供合规替代或 TikTok 书面确认 | 已采纳 |
| SA-003 | P0 | Token | 快照表无 scope/App 审核信息 | 数据库候选与 creator info 实测分层展示 | 已采纳 |
| SA-004 | P0 | 幂等 | init 超时可能重复发帖 | 有 `publish_id` 只对账；结果不明进入 `needs_review` | 已采纳 |
| SA-005 | P1 | 视频 | 新版片尾含示例 ID | GPU 显示真实 ID并标记教程示例 | 已采纳 |
| SA-006 | P1 | 部署 | 主服务和 X sidecar 线上来自不同分支 | 在独立整合分支合并并分别回归 | 已采纳 |
| SA-007 | P0 | 批量排期 | 现有队列禁止同账号同一发布时间重复；多个素材共用一个时间会从第二条开始冲突，并形成非预期部分成功 | 使用首条时间加可编辑正整数间隔，默认 10 分钟；不放宽数据库唯一约束 | 已采纳 |
| SA-008 | P0 | 部分失败 | 浏览器顺序调用可能因一项异常中断整批，或成功后清空失败上下文 | preview 和 queue 各自逐项捕获错误、继续后续项并保留汇总；已成功任务不回滚 | 已采纳 |
| SA-009 | P0 | 描述身份 | 批量使用首个素材的已渲染描述会把同一 Drama ID 写入其他任务 | 页面提交共享 `caption_template`，服务端为每个素材重新解析真实 `content_id` 后分别渲染和冻结 | 已采纳 |
| SA-010 | P0 | 幂等 | 解锁自定义模板后，若 Core 只比较素材/账号/时间，同一幂等键改文案可能静默返回旧任务 | Service 与 Core 均将模板和最终文案纳入冻结身份；变化时 409，精确重放返回旧任务 | 已采纳 |
| SA-011 | P1 | 长度 | Python 字符数与 TikTok 2200 UTF-16 单位不等，emoji 可能绕过服务端长度检查 | Core 按 UTF-16 单位校验，并加入 2200/2201 边界测试 | 已采纳 |
| SA-012 | P1 | GPU 成本 | 预览后建队再次使用不同 job 会重复下载和转码，批量时成倍放大 | preview/queue 使用相同确定性 prepare 身份；源、裁剪、资产或 profile 变化才失效 | 已采纳 |
| SA-013 | P1 | 兼容 | 直接废弃 `caption_text` 或按新默认模板重算会破坏旧调用方和历史任务重放 | 兼容旧 `caption_text` 与两字段缺省默认模板；精确历史重放在 GPU 前返回既有冻结任务 | 已采纳 |

## 决策记录

- CPU：页面、权限、快照只读、素材映射、队列、调度、审计。
- GPU：`/data` 成片、TikTok API 预检/发布/对账。
- CPU→GPU：loopback sidecar + SSH 反向隧道 + 专用 bearer；敏感 Token 使用 AES-GCM 短时信封且不落盘。
- 服务端媒体只使用 `PULL_FROM_URL`。
- 验收不创建真实 TikTok Post。
- 批量只在页面编排，后台继续接收单素材 preview/queue 请求；不增加批次事实表。
- ID 输入复用 X 素材池规则：1–100 个唯一正整数，支持空白、换行、中英文逗号和分号，首次出现顺序有效。
- 成功预览项先冻结时间序列，再逐条建队。预览失败不占槽；建队失败留下原槽，后续项不前移，保证重试身份稳定。
- 首条时间按 `Asia/Shanghai` 输入，间隔为 1–1440 分钟整数且默认 10 分钟。
- 描述输入的事实是模板，不是首个素材的最终文案；`{{contect_id}}` 和 `{{content_id}}` 为仅有合法占位符。
- 部分失败不等于整体回滚。页面必须给出逐项成功/失败和总数，操作人员能明确判断哪些任务已创建。

## PM 修订确认

2026-07-29 已将初版及本轮增量全部 P0/P1 建议写入需求和验收标准。本轮代码、自动化和生产关闭态验证尚未完成，当前结论不构成部署完成或开放 Direct Post 的证明。

## 2026-07-30 增量架构评审（仅本地）

### 结论

“TT 长素材 + 每日账号素材池 + 手动立即发布”方案在本地架构与自动化层面有条件通过。条件是继续保持 X 的 140 秒合同独立、固定 600 秒安全窗口、账号串行、两段崩溃恢复和三重门禁 fail-close。本结论不表示增量已经部署生产或线上验收通过。

### 增量决策

- TT 使用独立 resolver 接受 `1..3600` 秒素材；不得修改 X selector 的 `1..140` 参数。素材 4665764 的本地回归模型固定为 2087 秒。
- 每日排期只保存 `Asia/Shanghai` 的 `HH:MM` 和版本；素材池按账号隔离并以 `created_at,id` 为 FIFO 事实，不在页面计算下一条。
- 每个自然日时点由唯一 run 表示。重复 tick、进程重启和手动/自动竞争均必须回到同一持久化身份。
- 安全窗口固定为 600 秒。宽限内允许恢复同一 run；超窗只能 `missed`，不能追发。
- `claim → freeze` 通过 claimed-unbound run 恢复；`freeze → bind` 通过 legacy queue 的稳定幂等键恢复。恢复逻辑不得删除或重建旧 queue 状态机。
- 手动意图按 `account_id` 保存到 `sessionStorage` 的非敏感映射；成功或明确未发布删除对应 key，`unknown`/未确认保留。页面刷新或切换账号不得生成跨账号重复请求。
- Runner 先消费旧 queue 的领取预算，再持久化 daily due；仅以剩余 `claim_limit` 领取新建 daily queue。reconcile 始终在领取/发布之后。
- 三道 Direct Post 门禁在 recurring claim 前检查。门禁关闭必须做到“不消费素材”，而不只是“不调用远端 API”。
- 同一 run 的执行权使用 120 秒独占 lease 和 fencing token；`freeze/release/bind` 必须在事务内同时核验 run、pool、lease 与 token 身份，防止过期 owner 产生错误释放或孤儿 queue。
- 账号切换进入未配置或加载态时，发布时间必须先恢复默认 `11:00`，不得复用上一账号的展示值。
- 主应用公共兼容精确 `/api/admin/tt-posts/queue` 只允许 GET；创建只能走受控的新流程，动态 cancel/reconcile 路由继续保留。

### 增量问题闭环

| 编号 | 级别 | 问题 | 决策 | 本地状态 |
| --- | --- | --- | --- | --- |
| SA-014 | P0 | 为 TT 放宽长视频可能连带放宽 X | TT resolver 独立 3600；X selector 参数固定 140 并单独回归 | 自动化通过 |
| SA-015 | P0 | minute tick 重复可能重复消费 FIFO | 自然日时点唯一 run + 账号级 FIFO + 账号 active-run 串行 | 自动化通过 |
| SA-016 | P0 | claim 后或 queue 冻结后进程退出可能留下半状态 | 分别持久化 claimed run，并按 queue 幂等键恢复 bind | 自动化通过 |
| SA-017 | P0 | 手动请求响应丢失、刷新或切号可能重复消费 | sessionStorage 按账号持久化 key；unknown/未确认不换 key | 自动化通过 |
| SA-018 | P0 | 旧队列与 daily queue 同 tick 可能把 `claim_limit` 翻倍 | 旧队列先 claim，daily 只使用剩余预算 | 自动化通过 |
| SA-019 | P0 | 门禁关闭后先 reserve 再拒绝会占住素材 | 门禁检查前置到 recurring claim，关闭时 pool 保持 `available` | 自动化通过 |
| SA-020 | P1 | 可配置宽限会造成多节点语义漂移 | Runner 与 sidecar 仅接受 600 秒 | 自动化通过 |
| SA-021 | P0 | 同一 run 的并发 owner 在预检异常与 queue 冻结交错时可能错误释放素材或留下孤儿 queue | 每 run 120 秒 execution lease + fencing token；freeze/release/bind 事务内校验完整身份，过期 owner 全部失权 | 自动化通过，本地关闭 |
| SA-022 | P1 | 切换未配置账号时可能继承上一账号的发布时间 | 未配置/加载态统一先重置默认 `11:00`，只渲染当前账号的有效配置 | 自动化通过，本地关闭 |
| SA-023 | P1 | 公共兼容 POST `/queue` 在门禁关闭时仍可能 reserve 素材 | 主应用精确 `/queue` 改为 GET-only；保留 GET 与动态 cancel/reconcile | 自动化通过，本地关闭 |
| SA-024 | P0 | 4665764 通过时长校验后，规范化成片超过旧 2 GiB 合同 | 源下载继续限制 2 GiB；最终成片按 TikTok Content Posting 官方上限调整为 4 GiB | 自动化通过，生产复测中 |
| SA-025 | P1 | oneshot runner 与 sidecar 共用 `RuntimeDirectory` 时可能删除手动 kick 目录 | `/run/tt-post` 只由常驻 sidecar 持有；runner 复用但不声明所有权 | 自动化通过，生产复测中 |
| SA-026 | P1 | ready manifest 在生成后若运行配置收紧或 profile/对象身份变化，旧缓存可能绕过当前媒体合同 | prepare 与 publish 共用响应校验；每次复用都按当前 `max_output_bytes`、profile、job/content 身份、规范化 probe、SHA 和精确 COS URL 全量复验，任一不符 fail-close | 自动化通过，本地关闭 |
| SA-027 | P1 | 公网通用 `/api/admin/` 旧 300 秒、主应用通用 600 秒及 CPU 侧旧 GPU 900 秒窗口均小于 4665764 首次转码加 2.36 GB COS 分片上传耗时，上游可能先超时而 GPU 仍在完成同一 job | 按调用层级留余量：CPU 到 GPU 3600 秒，主应用 exact preview `TT_POST_ADMIN_PREVIEW_TIMEOUT=3660`，nginx exact preview read/send 3720 秒；其他主应用路由仍为 600 秒，其他 admin API 不变、三项门禁不变，确定性 job 重试复用 ready 成片 | 自动化通过，生产复测中 |

### 生产验收待填写

- 合入提交、不可变 release、备份和回滚点：`待填写`
- 七表迁移、生产数据完整性和旧四表兼容：`待填写`
- 生产 Runner timer/path、先旧队列后 due、总 claim 预算：`待填写`
- 三项门禁值与“不消费素材”的生产证据：`待填写`
- 素材 4665764 的真实 resolver/GPU/账号时长链路：`待填写`
- 登录态跨刷新、多账号手动 key 行为及无凭据存储检查：`待填写`
