# 测试用例

2026-11-04；以下状态为设计覆盖，实际命令和结果统一写入 `test-report.md`。

## 测试范围

FB 原生 manual/Excel/Page pool/group pool/旧自动发布与统一调度之间的提交、领取、数据库后备约束、恢复、只读对账及历史升级。测试不向 Meta 创建验收帖；线上仅执行用户明确授权的既有任务恢复；15 条跳过行由用户重传原 Excel，不自动补发。

## 测试数据

使用临时 SQLite、可控时钟与计数 Graph fake。Page P 的素材 A 为故障任务，素材 B/C 为后继；Page Q 用于隔离对照。同时覆盖 manual 和 auto 任务。记录 Graph POST、GET、凭证发放次数，核对 task/operation/短链身份。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-01 | 新 Excel 不同素材 | P/A unknown；P/B 无历史 | 校验并提交 B | B 可入队；A 原状态和短链不变 | P0 | 待执行 |
| TC-02 | 同素材重复未知 | P/A unknown 或 unknown_outcome=1 | 同/不同批次再次提交 A，含 allow_repeat | 拒绝 A；Graph POST=0 | P0 | 待执行 |
| TC-03 | 跨模块/租户防重 | auto P/A 未决，manual P/B 正常 | manual 提交 A/B；反向重复 | A 拒绝、B 可用，租户和模块切换不绕过 | P0 | 待执行 |
| TC-04 | 已知 ID 后继 | P/A submitted、Meta processing | 领取 P/B 并对账 A | B 可写；A 只有 GET，不再次 POST | P0 | 离线通过 |
| TC-05 | 在途写互斥 | P/A running 且租约有效 | 两连接并发领取 B/C | 没有第二个同 Page running POST；Q 不受影响 | P0 | 离线通过 |
| TC-06 | 同素材并发提交 | P/A 无历史 | 两连接同时提交同素材，跨模块复测 | 至多一个未决素材任务被接受 | P0 | 待执行 |
| TC-07 | 前序未决不阻塞 | 同批 A unknown、B ready | 正常领取 B | 领取 B，A 不自动重试 | P0 | 离线通过 |
| TC-08 | 前序准备/短链卡住 | 同批 A preparing 或 link pending，B ready | 领取/调度 B | B 可执行；A 保留原加工/链接任务身份 | P1 | 离线通过 |
| TC-09 | 正常就绪顺序 | A/B 均 ready 且到期，A 更早 | 顺序领取 | 保留可执行任务的原有时间顺序；未来任务不提前 | P1 | 离线通过 |
| TC-10 | 准备与发布隔离 | P/A prepare 运行或未知，P/B ready | 中心领取 B | 长准备不占用 Page POST 资源；原 GPU 容量限制保留 | P1 | 离线通过 |
| TC-11 | 无写意图租约过期 | 原生 running 过期；所有写入账本为空 | tick/中心恢复两次 | 有界安全恢复或明确未提交失败；幂等、不新建任务/短链 | P0 | 离线通过 |
| TC-12 | 存在任一写入证据 | native started / FB operation / 中心 intent 任一存在 | 租约过期恢复 | 原任务保持未知或仅对账；POST 不增加 | P0 | 离线通过 |
| TC-13 | 中心先过期 | 中心 120s 租约过期、原生 POST 仍有有效租约 | expire 后领取同 Page B | 原生在途写保护有效；旧 generation 不获新凭证 | P0 | 离线通过 |
| TC-14 | 临时预检失败 | 无凭证发放、无 operation，资料库连接故障 | 故障解除后恢复、重复投影 | 原失败审计保留，安全恢复不因 attempt 总行数变 unknown | P1 | 离线通过 |
| TC-15 | 预检长期失败 | 临时来源错误持续发生 | 重复扫描已明确失败任务 | 明确失败可见且不自动无限重试；不占整 Page、不形成请求风暴 | P1 | 待执行 |
| TC-16 | 明确 Meta 拒绝 | Graph 返回结构化明确失败 | 同任务按原 Token 规则执行 | 原有明确失败轮换生效；成功或未知立即停止 | P0 | 离线通过 |
| TC-17 | 晚到成功回执 | A 已 unknown，B 已 running | 交付 A 原 operation 的成功 ID 两次 | A 进入只读对账且幂等；B 租约和资源不受影响；不新发 A | P0 | 离线通过 |
| TC-18 | 无 ID 未知 | A 网络超时，无对象 ID | 运行自动对账/恢复 | 不猜成功/失败、不重新 POST；B 仍可执行 | P0 | 离线通过 |
| TC-19 | 已知 ID 正常对账 | submitted 带 ID，Graph ready/published | 通过现行中心路径对账 | 原生/中心最终 published，保留同一 ID，清本任务待定占用 | P0 | 离线通过 |
| TC-20 | 对账暂不可用 | Graph GET 超时/凭证暂失效 | 多轮对账 | 原任务保留 ID 和不确定事实；B 可用；不 POST A | P0 | 离线通过 |
| TC-21 | 对账身份不匹配 | 返回不同对象 ID 或凭证归属不符 | 执行对账/恢复 | 拒绝错误归账；不换成他人 Token；不释放同素材保护 | P0 | 待执行 |
| TC-22 | 旧 DB 迁移 | 旧 partial UNIQUE、旧 triggers、Page unknown 隔离资源 | 升级、重启、再升级 | 原行/短链不变；新约束生效且迁移幂等 | P0 | 离线通过 |
| TC-23 | 旧账本独立未知 | auto task 已变化但 publish ledger 仍未决 | 新提交 A/B | A 仍保护，B 正常；不遗漏历史残留 | P0 | 离线通过 |
| TC-24 | 多个素材未决 | P/A 和 P/B unknown、P/C 正常 | 迁移/领取/恢复 A | C 可用；处理 A 不释放 B 的素材锁 | P0 | 离线通过 |
| TC-25 | 池不中断 | Page/group pool 当前素材未决，下一素材可用 | 下一正常时点取材 | 当前条目独立处理；池不因素材级异常永久暂停 | P1 | 待执行 |
| TC-26 | 已跳过 15 行不擅自补发 | 21 行已入队，15 行被跳过；用户选择自行重传 | 修复并核对本批子任务和恢复审计 | 不自动创建跳过的 15 条，不重复原 21 条；原设置由用户原 Excel 保留 | P0 | 待执行 |
| TC-27 | 原 Excel 重新校验 | 原跳过 Page 已修复；使用原 Excel 行字段 | 只读校验样例；已有活动项另行核对 | 原文案/标题/Token/预约不被后台推断替换；不同素材不再因旧unknown被整页跳过 | P1 | 待执行 |
| TC-28 | 其他渠道回归 | X/TT/YouTube 未知或在途 | 运行原调度/恢复测试 | 锁范围、代次、授权和 unknown 禁重发行为不变 | P0 | 待执行 |
| TC-29 | 发布中升级/回退 | 存在真实 POST、多个新 submitted/unknown | 演练部署、兼容回退 | 不终止在途写、不恢复旧数据库、不重新建立不兼容 Page 唯一约束 | P0 | 待执行 |
| TC-30 | 持续运行验收 | 新版运行，目标 Page 故障旧任务保留 | 正常后继实际执行并读取平台结果 | 后继正常，目标专属短链对应视频不重复；审计可复核 | P0 | 待执行 |
| TC-31 | 历史缺失素材身份 | unknown 的 material_id 为空/非法；快照有或无唯一真值 | 投影、迁移、提交和领取 | 仅唯一可验证身份获得精确素材锁；无法解析不虚构身份或无条件解除保护 | P0 | 离线通过 |
| TC-32 | 有界 GET 截断授权 | 授权池超出单轮上限，未试授权仍可能有效 | 该轮已试授权全部拒绝 | 保持 submitted 并退避，不宣称全部授权拒绝，不新增 POST | P1 | 离线通过 |
| TC-33 | malformed 仅影响所在 Page | 两 Page 同一 run，其中一 Page 存在无法识别素材的未知历史 | create_run | 受影响 Page 明确 skipped，另一 Page planned，不整体回滚 | P1 | 离线通过 |
| TC-34 | 准备租约过期 | auto preparing 过期，分别无写证据/存在写证据 | maintenance_tick 重复执行 | 无写证据明确失败，有写证据保留 unknown；有效租约不改变 | P0 | 离线通过 |
| TC-35 | 迟到准备仍保持批次间隔 | 后序行先 published，前序行后来 ready | 立即及间隔后领取前序行 | 基于该 batch+Page 最新成功时间等待原 interval，防止密集发布 | P1 | 离线通过 |
| TC-36 | malformed 历史不挡既有回执 | 同 Page 跨族有 failed+unknownflag 且缺素材 ID 的历史 | 既有任务 GET/receipt；同时尝试新 running、INSERT、改素材 | 已有事实可落账，新写/新增任务仍被保护；过期 running 可安全归 unknown | P0 | 离线通过 |

## 回归范围

- 原生批次、Excel、pool/group scheduler、Graph transport、跨模块 guards 和存量迁移。
- 统一调度 claim/expire/fail/reconcile、历史回执、资源分配、prepare/publish 隔离、多个节点并发。
- 旧发布事实、来源冻结、预约时间、归属、短链和 operation 幂等不变；无关渠道及 Page 的队列不被重置。
