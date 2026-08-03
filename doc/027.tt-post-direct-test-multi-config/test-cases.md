# 测试用例

## 测试范围

- D：独立 direct-test、重复素材、幂等、每任务新 GPU job 和 unknown 阻断。
- P：素材发布状态、自动池状态和自动不复用。
- C：描述模板、总开关/时间、多账号的原子配置与 UI 成员状态。
- S：同分钟全部 due slot 的 existing-claim 预占、执行和崩溃恢复。
- M：旧逐账号排期迁移与向旧版本回滚兼容。
- N：权限、脱敏、无真实 Post、无生产配置写入和外部状态不变。

所有自动化使用临时 SQLite、fake account repository、fake creator-info、fake GPU/COS/TikTok。生产浏览器验收只允许 GET/静态页面与只读 SQL，禁止调用任何写接口。

## 测试数据

| 标识 | 说明 |
| --- | --- |
| MAT-U | 校验成功、从未发布、未加入自动池的素材 |
| MAT-P | 至少一条 queue 或 direct-test 已确认 `published` 且有 publish ID 的素材 |
| MAT-F | 自动 pool 已 consumed，但对应 queue 明确 failed 的素材 |
| MAT-X | 没有 confirmed published，存在 `unknown` direct-test 的素材 |
| MAT-B | 自动池 `available` 且显式归属账号 A 的素材 |
| MAT-ACT | 存在 preparing/publishing direct-test 的素材 |
| A | 有效账号，账号设置完整，creator-info 可用 |
| B | 有效账号，账号设置完整，与 A 使用同一分钟 |
| C | 有效账号，但没有可用自动素材 |
| U | 已在旧配置中、当前账号源不可用的账号 |
| BAD | 请求中伪造或不存在的账号 ID |
| CFG-7 | 已保存自动配置 version=7，模板含 Drama ID、`{url}`、`{desc}` |
| LEGACY-ONE | 多个旧账号都只在 11:00 启用，单例配置不存在 |
| LEGACY-MULTI | 账号 A=11:00、B=11:10，单例配置不存在 |
| KEY-1/KEY-2 | 两个不同 direct-test 幂等键 |
| DB-COPY | 生产 online backup 的隔离副本，已脱敏且 runner 不连接 |

## 执行前基线与禁令

1. 记录 config 内容/version、所有 legacy schedule、pool/intake/queue/run/direct-test 行数与状态、短链 wrapper 清单、GPU manifest/publish-ledger 文件数和已知 TikTok Post ID 集合。
2. fake 环境必须证明 GPU publish 调用计数可观测；默认设置为一旦调用即使测试失败。
3. 生产验收不得调用：`POST /auto-config`、`POST /material-pool`、`POST /test-publish`、旧 `POST /run-now`、任何 `/internal/*publish*` 或 schedule-save。
4. 不通过关闭生产 schedule、修改生产门禁或消费生产素材来隔离测试；所有可写用例只在临时库执行。

## D. 独立 direct-test

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| D01 | 未入池素材可测试 | MAT-U、A、CFG-7 | POST `/test-publish` | 返回 200/queued；服务端重解析素材；未要求 pool item | P0 | 待执行 |
| D02 | 已发布素材可重复测试 | MAT-P、A、KEY-1 | 创建 direct-test | 新 direct-test 成功；历史 published 行不变 | P0 | 待执行 |
| D03 | 相同素材显式第二次测试 | D02 明确终态 | 用户点“再发一次测试”生成 KEY-2 | 新 task ID、新 gpu_job_id；不复用 D02 prepare 结果 | P0 | 待执行 |
| D04 | 同键同请求幂等 | 用 KEY-1 提交成功 | 原账号/素材/version/consent 重复 3 次 | 返回同 task/job；只一行、一份 prepare；重放不调账号/素材/TikTok 依赖 | P0 | 待执行 |
| D05 | 同键异素材冲突 | KEY-1 已绑定 MAT-U | 改 MAT-P 重放 | 409 `tt_post_direct_test_idempotency_conflict`；原行不变 | P0 | 待执行 |
| D06 | 同键异账号或 consent 冲突 | KEY-1 已绑定 A 与原 consent | 分别改为 B、改 consent version/accepted_at 重放 | 均 409；无新任务或 GPU 调用 | P0 | 待执行 |
| D07 | 非终态 key 生命周期 | 服务已建任务，响应为 queued/preparing/ready/publishing/reconciling 或客户端超时 | 刷新/二次点击并查询列表 | 始终复用原 key/version/consent；不生成新 job；终态后仅显式再测才换 key | P0 | 待执行 |
| D08 | unknown 禁止绕过 | MAT-X 有 unknown direct-test | 同 key 重放；再用 KEY-2/MAT-X 创建 | 原 key 返回 unknown；新 key 409；0 新任务/GPU 调用 | P0 | 待执行 |
| D09 | 不误锁同账号其他素材 | A 有 MAT-X unknown，MAT-U 无活动事实 | 用 MAT-U 对 A 测试 | 创建成功；阻断粒度是素材，不是账号；发布 claim 仍串行 | P0 | 待执行 |
| D10 | 同素材活动任务阻断 | MAT-ACT 在 A preparing/publishing | 用 B 对同素材创建测试 | 409 active；原任务继续，不生成新 job | P0 | 待执行 |
| D11 | 明确失败后可新测 | A 上一测试明确 failed、无 unknown | 新 key 创建 | 新任务允许；新 gpu_job_id | P1 | 待执行 |
| D12 | 测试目标为空/数组/多个 | MAT-U | 分别提交空、数组、两账号 | 均 400；0 direct-test、0 GPU | P0 | 待执行 |
| D13 | 测试不从成员首项推断 | CFG-7 选择 A/B，未传目标 | 创建测试 | 400；服务端不使用 A 或 B | P0 | 待执行 |
| D14 | 非自动成员可测试 | A 不在自动成员但有效，CFG-7 已保存 | 显式对 A 测试 | 成员/开关不构成阻断；version 只冻结模板；仍执行设置/creator-info/门禁 | P0 | 待执行 |
| D15 | 非法/失效账号 | BAD 或失效账号 | 创建测试 | 404/409；0 direct-test、0 prepare、0 pool 变化 | P0 | 待执行 |
| D16 | 配置版本冲突 | 客户端 CFG-7，服务端已 version=8 | 创建测试 | 409；不使用旧模板，不创建任务 | P0 | 待执行 |
| D17 | 冻结不漂移 | 任务创建后修改 config/素材源/账号设置 | 继续 prepare/publish fake 流程 | 使用任务冻结事实；不读取新值改写 caption/目标 | P1 | 待执行 |
| D18 | 每任务新 prepare job | MAT-P 连续两次明确测试 | 比较 GPU 请求/ledger key | job ID 不同；既有 GPU ledger 代码/旧文件不改写 | P0 | 待执行 |
| D19 | prepare 失败 | fake GPU prepare 明确失败 | 跑 prepare runner | direct-test=failed；0 queue/run/pool 变化；0 publish 调用 | P0 | 待执行 |
| D20 | publish 不确定 | fake publish 超时且无法核对 | 跑 publish runner | direct-test=unknown；不自动重试；同素材新测试被阻断 | P0 | 待执行 |
| D21 | unknown 内部核对成功 | D20，fake GPU ledger 已出现明确 publish 事实 | 调内部 reconcile | 原任务转 published 并写 publish 字段；解除同素材阻断；不依赖 `tt_post_direct_test_event` | P0 | 待执行 |
| D22 | reconcile 无明确证据 | unknown 但 GPU/远端仍不明确，或任务非待核对 | 调内部 reconcile | 保持 unknown/合法原状态；不猜测 published/failed，不新发 Post | P0 | 待执行 |
| D23 | direct-test 与自动池隔离 | MAT-B，保存 pool 字段基线 | 跑测试到任意终态 | pool status/run_id/queue_id/created/updated/FIFO 均不变 | P0 | 待执行 |

## P. 素材发布状态与 auto/direct 互斥

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| P01 | queue 确认发布 | queue=published 且有 publish ID | preview/pool GET | publication_state/status=published，publish_count=1 | P0 | 待执行 |
| P02 | direct-test 确认发布 | direct-test=published 且有 publish ID | preview/pool GET | publication_state/status=published，最新字段来自事实 | P0 | 待执行 |
| P03 | consumed 但失败 | MAT-F | preview/pool GET | publication=unpublished；pool status 仍 consumed；不误标已发布 | P0 | 待执行 |
| P04 | 结果未知 | MAT-X，无 published | preview/pool GET | publication=unknown、unknown_count>0 | P0 | 待执行 |
| P05 | 历史已发布又有 unknown | MAT-P 后续 unknown | preview/pool GET | 主状态仍 published；publish_count>0 且 unknown_count>0 | P0 | 待执行 |
| P06 | 活动处理中 | MAT-ACT，无 published/unknown | preview/pool GET | publication=unpublished、attempt_count>0；不存在 processing 枚举 | P1 | 待执行 |
| P07 | 无历史 | MAT-U | preview | state=unpublished；可明确选择测试 | P1 | 待执行 |
| P08 | 自动不重新领取 consumed | MAT-F pool=consumed | 运行自动 claim | 不重置 available、不新 queue | P0 | 待执行 |
| P09 | direct published 不永久阻断自动池 | pool available，已有 direct-test published | 运行自动 claim | 可按原账号/FIFO 领取；direct-test 历史不变，pool 只发生正常自动状态迁移 | P0 | 待执行 |
| P10 | 自动排除活动/unknown 测试 | pool available，分别 active/unknown | 运行自动 claim | 两者均不领取；unknown 需核对 | P0 | 待执行 |
| P11 | 其他测试终态恢复 auto eligibility | pool available，测试分别 failed/canceled | 运行自动 claim | 均可按原 FIFO 领取一次 | P1 | 待执行 |
| P12 | 自动池和发布状态双展示 | 构造四类组合 | 查看 UI/API | 两个维度独立，不把一个 badge 复用为另一个 | P1 | 待执行 |

## C. 原子配置、多账号与 UI

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| C01 | 一次保存三部分 | version=7，A/B 有效 | 改模板、启用 11:00、选 A/B 并保存 | 一个事务成功，version=8；三部分同快照 | P0 | 待执行 |
| C02 | 单一版本 | C01 完成 | 读取 config/accounts/legacy schedules | 只有 config version；成员无独立可冲突版本 | P0 | 待执行 |
| C03 | 版本冲突 0 写入 | 两客户端均读 version=7，A 先保存 | B 用 version=7 保存不同全部字段 | 409；模板/开关/时间/成员/schedules 都保持 A 结果 | P0 | 待执行 |
| C04 | 末尾账号无效 0 写入 | 49 个有效 + BAD | 开启保存 | 整批失败；前 49 个 schedule 也未写 | P0 | 待执行 |
| C05 | 账号无设置/能力不兼容 | A 有效、B 无设置或禁止策略 | 开启 A/B | 409；config/member/schedule 0 写入 | P0 | 待执行 |
| C06 | creator-info 中途失败 | A 成功、B 超时 | 开启 A/B | 整批失败；不使用过期能力部分保存 | P0 | 待执行 |
| C07 | 纯关闭不依赖远端 | config 开启且含失效 U，账号源断开 | 保持模板/时间/成员，enabled=false 保存 | 成功关闭；无需 creator-info/新 consent/门禁 | P0 | 待执行 |
| C08 | 关闭并移除失效成员 | C07 前态 | enabled=false 且移除 U | 成功；其余值原子保存 | P0 | 待执行 |
| C09 | 关闭态新增成员受信任 | config 关闭且已有有效 consent | 新增可信且有本地设置 B；再新增 BAD/无设置账号 | B 可保存且无 creator-info；BAD/无设置 409 且该次 0 写入 | P0 | 待执行 |
| C10 | 关闭态改模板校验 | config 关闭 | 保存非法宏/超 2200 模板 | 400；成员/开关/schedule 0 写入 | P0 | 待执行 |
| C11 | 空账号开启 | enabled=true，members=[] | 保存 | 400 `tt_post_auto_accounts_required`；0 写入 | P0 | 待执行 |
| C12 | 账号重复/超过 50 | 构造重复或 51 个 ID | 保存 | 400；0 写入 | P0 | 待执行 |
| C13 | 多时间被拒绝 | 请求两个分钟 | 保存 | 400；v1 不接受多个时间，0 写入 | P0 | 待执行 |
| C14 | 成员布尔和状态值 | 配置启用且含可用 A、不可用 U、未选 B | GET/UI | A=true+active；U=true+attention_required；B=false+not_selected；均带 config version | P1 | 待执行 |
| C15 | dirty draft 防覆盖 | 修改模板/多选/时间未保存，后台轮询返回旧值 | 等待轮询 | 三处草稿均保留并标记未保存 | P1 | 待执行 |
| C16 | 保存中控件锁定 | 发起慢保存 | 点击账号/模板/开关和再次保存 | 控件锁定；只一请求；旧 GET 不覆盖结果 | P1 | 待执行 |
| C17 | 409 后刷新 | C03 | UI 处理冲突 | 丢弃旧草稿，加载最新 version，并要求重新确认 | P1 | 待执行 |
| C18 | 自动素材明确归属 | 自动成员 A/B | 入池未传 owner、传数组、传 A | 前两种拒绝；传 A 后每素材 account_id=A | P0 | 待执行 |
| C19 | 不静默取首账号 | 多选顺序 B/A，入池无 owner | 提交 | 400；A/B 均无 intake | P0 | 待执行 |
| C20 | 配置模板是事实源 | config version=8，UI 有未保存草稿 | 入池或测试 version=8 | 使用 version=8 模板；不使用草稿 | P0 | 待执行 |
| C21 | 历史冻结 | 保存 version=9 新模板/成员 | 读取 version=8 pool/queue/test | 历史 caption/账号/短链完全不变 | P1 | 待执行 |
| C22 | 账号设置不被配置保存修改 | A/B 有不同 privacy/interaction version | 保存 auto config | `tt_post_account_setting` 内容/version 均不变 | P0 | 待执行 |

## S. 同分钟全部 due

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| S01 | 两账号同分钟允许 | A/B 均 11:00 | 保存配置 | 成功，不再报 time conflict | P0 | 待执行 |
| S02 | 50 账号先全量预占 | 50 成员均 11:00 且各有素材，limit=1 | tick 一次 | 首个 creator-info 前完成 50 次 claim；50 个 run/reservation，执行 items≤1 | P0 | 待执行 |
| S03 | tick 重入去重 | S02 后相同时间调用 3 次 | 检查 run key/pool reservation | 仍 50 个稳定 run，无重复 pool claim/queue | P0 | 待执行 |
| S04 | 单 slot 预占事务失败 | 第 25 个 claim 注入 DB 错误 | tick | 第 25 个 0 写入；其余 slot 仍尝试；任何 creator-info 都晚于整个预占循环 | P0 | 待执行 |
| S05 | 当前 due 先于旧 recovery | 已有 claimed/unbound recovery，当前另有 50 due slots | tick 并在首个 creator-info 观察 | 先完成当前 50 个 claim，再执行旧 recovery；不重复领取 | P0 | 待执行 |
| S06 | 一个账号无素材 | A 有素材、C 无素材 | 同分钟 tick | A 建 run/reservation；C 返回 skipped 且不建空 run；A 可继续 | P0 | 待执行 |
| S07 | 一个账号预检失败 | A/B 均已预占，B creator/settings 失败 | 执行已预占 run | B 失败不回滚/阻塞 A；网络调用发生在全量预占后 | P0 | 待执行 |
| S08 | 每账号只取自己素材 | A/B pool 各一条 | 同分钟处理 | A 只取 A，B 只取 B；无跨账号 fallback | P0 | 待执行 |
| S09 | 配置版本冻结 | run 预占后保存新时间/成员 | 处理旧 run | 使用 run 的旧 config version/账号，不重复生成新 slot | P1 | 待执行 |
| S10 | unknown 不影响其他 slot | A 有 unknown，B 正常且同分钟 | tick/处理 | A fail-closed 并保留证据；B 已预占且可处理 | P0 | 待执行 |

## M. 旧排期迁移与回滚兼容

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| M01 | additive migration 两次 | DB-COPY | 启动 migration 两次 | 两次成功；第二次 0 变化；旧表/行/索引完整 | P0 | 待执行 |
| M02 | migration 不自动写配置 | 有旧 schedule，单例不存在 | 只运行 migration | 新表为空；旧 schedule/version 不变 | P0 | 待执行 |
| M03 | 单一旧时间投影 | LEGACY-ONE | GET auto-config | version=0、预填 11:00、模板标记未保存；无 DB 写 | P1 | 待执行 |
| M04 | 多旧时间 review | LEGACY-MULTI | GET auto-config | publish_times=[]、逐账号旧时间、legacy_review_required=true/mode=mixed | P0 | 待执行 |
| M05 | 禁止时间并集交叉 | LEGACY-MULTI | 尝试不选共同时间直接保存 | 409；A 仍 11:00、B 仍 11:10；无 A+B 双时间 | P0 | 待执行 |
| M06 | mixed 两步迁移 | LEGACY-MULTI，明确选 12:00/A+B | 先 version=0 disabled 保存，再 version=1 enabled | 首次 version=1 且关闭/A-B 12:00；第二次 version=2 启用 | P0 | 待执行 |
| M07 | 首次迁移失败回滚 | M06 中注入末尾 schedule 错误 | 保存 | singleton 不存在；A/B 旧时间/version 完整保留 | P0 | 待执行 |
| M08 | 回退旧代码可读 | M06 成功后的 DB 副本 | 启动上一 release 只读检查 | 旧代码忽略新表，读取 A/B 共同 legacy schedules，无 schema 错误 | P0 | 待执行 |
| M09 | rollback 保留新任务 | DB 有 published/unknown direct-test 和 GPU ledger | 回退代码/静态 | SQLite/ledger/manifest/COS 不删除不覆盖；任务供前滚核对 | P0 | 待执行 |
| M10 | rollback 不恢复旧 SQLite | 有部署前 backup 和部署后历史 | 执行正常回滚演练 | 只切 release；当前 DB inode/hash/history 保留 | P0 | 待执行 |

## N. 安全、权限和无副作用验收

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| N01 | 未登录/无权限 | 各新 API | GET/POST | 401/403；0 写入 | P0 | 待执行 |
| N02 | 同源与未知字段 | 写 API | 跨站、错误 content-type、注入字段 | 拒绝；0 写入/0 GPU | P0 | 待执行 |
| N03 | Token/Secret 脱敏 | 构造上游错误含凭据 | 查看 API/audit/UI | 响应和日志均无敏感值 | P0 | 待执行 |
| N04 | 自动化无真实 Post | 临时 DB + fake GPU | 执行全部 D/P/C/S/M 用例 | 真实 publish endpoint 调用=0；生产 ledger/Post 不变 | P0 | 待执行 |
| N05 | 生产只读验收 | 保存生产基线 | 只打开页面、GET、只读 SQL/health | 无 POST 写请求；配置/pool/queue/run/test/ledger/Post 基线相同 | P0 | 待执行 |
| N06 | 不保存生产配置 | 浏览器登录生产 | 检查多选/状态/迁移提示后退出 | 不点击保存；config 与 legacy schedule version/值不变 | P0 | 待执行 |
| N07 | 旧 run-now 兼容 | 部署候选 + 临时 DB/fake 上游 | 调旧接口并审计 UI | 兼容路由仍可用；新 UI 立即测试从不调用它，只调用 `/test-publish` | P0 | 待执行 |
| N08 | 三份 UI 一致 | 候选 release/后台/Nginx | 比较 SHA-256 | 三份完全相同；页面调用 `/test-publish`、`/direct-tests`、`/auto-config` | P1 | 待执行 |
| N09 | GPU ledger 不改协议 | fake + 旧 ledger fixture | 两次重复素材测试 | 仅出现不同 job key；旧 ledger 内容/hash 不变 | P0 | 待执行 |
| N10 | COS/短链隔离 | fake storage 和 TT namespace | 重复测试/失败/回滚 | 只用 TT 专用 COS/TT 短链；X 和其他桶行为不变 | P1 | 待执行 |

## 回归范围

- 018/021/022：素材校验、异步 prepare、自动池 FIFO、每素材单账号归属和 GPU 本地拉取。
- 019/020：账号设置单个/批量保存、creator-info 能力交集、隐私/互动设置版本不被自动配置修改。
- 023/024：旧一次性 canary、direct_clean profile 和正式三重门禁不被绕过。
- 024/026：`{{content_id}}`/`{{contect_id}}`、`{url}`、`{desc}`、UTF-16 2200、短链和片尾合同。
- 025：自动发布可纯关闭、失效账号占位、dirty draft、409、后台轮询和 no-side-effect 浏览器验收。
- X 发布链路、X 短链 namespace、其他 COS 桶和 Meta/其他后台功能不变。

## 通过门槛

- 所有 P0/P1 用例通过；开放 P0/P1 缺陷为 0。
- 任一错误用例必须同时断言错误码和“0 非预期副作用”，不能只断言 HTTP 状态。
- migration 在生产副本上幂等运行两次，并证明无旧排期时间交叉放大。
- 生产只读验收前后 config/schedule/pool/queue/run/direct-test/GPU ledger/已知 Post 基线一致。
