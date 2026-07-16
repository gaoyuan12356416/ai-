# 008.ad-control-v3-dynamic-ui 需求与技术设计

## 1. 结论与版本边界

本需求新增一套与线上自动调控 V2 完全并行的 V3。V2 页面、API、SQLite、runner、cron 和既有 Meta 行为不迁移、不替换、不停用。

本期可交付能力固定为：

- Facebook；
- 后端动态渲染的“规则组管理”和“执行日志”两页；
- 产品多选 + 一个优化师 + 可选账户时区范围；
- `campaign`、`adset`、`ad` 三种对象层级；
- `pause`、`copy` 规则的配置；
- 三层对象的手动 observe 试算；
- V3 配置、Preview、目标和执行审计写入 `ads_ai` 八张 V3 表；
- Preview gzip 快照、配置和发布备份使用数据盘。

本期明确不发布：计划调度器、规则启用、正式 pause、正式 copy、TikTok 扫描/执行、复制结果 `created_data/lineage/intent` 写入和快照清理器。所有未发布入口必须失败关闭，不能仅依赖 UI 隐藏。

## 2. 用户目标

- 新版界面只保留两个功能页，流程清晰、响应式、无业务字段默认值。
- 筛选范围不再依赖账户/账户池，由短剧产品多选与优化师共同确定。
- 普通优化师只能给本人建规则；admin 可以给任一有效优化师建规则。
- 同一套规则模型支持 Campaign、Ad Set、Ad，并为后续 TikTok 留出 channel adapter。
- 先通过手动 observe 验证范围和规则结果，保持 Meta 写入为 0。

## 3. 页面与入口

V3 只有两个动态页面：

- `/api/ad-control/v3/ui/rule-groups`
- `/api/ad-control/v3/ui/execution-logs`

页面由 Python 模板动态返回，业务 JS/CSS 由受权限保护的后端 asset 路由返回；仓库和 Nginx public root 不新增 V3 静态 HTML。导航只增加一个 V3 分组和上述两个动态链接，旧 V2 导航仍保留。

两页均要求 cookie 登录和 `ad_control_center` 模块权限。新页面固定显示：旧版独立运行、本期只支持保存草稿与手动试算、调度器未发布、启用锁定。

## 4. 范围模型

### 4.1 渠道

- `facebook`：本期启用配置、范围估算和手动 observe。
- `tiktok`：仅返回 disabled 能力元数据；保存、估算、试算和启用均返回 `channel_not_enabled`。

### 4.2 产品

- 产品 `value` 与 `kunlunads_dev.ads_custom_source_insight.product` 精确一致，不做大小写、别名或模糊替换。
- UI 支持多选；可用值来自 `ads_ai.ad_control_v3_product_catalog` 中 `channel=facebook`、`product_type=short_drama`、启用的记录。
- 本期审核 seed 为 15 个短剧产品枚举；生产执行 seed 前仍需核对 SQL 和当前业务枚举。
- 大表候选查询必须先锁定 `data_source IN (0,6)`、单个产品、`dt` 窗口、`optimizer` 和 `platform=0`，按产品拆分并强制使用 `dpdo(data_source,product,dt,optimizer)`；每次源连接先设置 `SET SESSION max_execution_time=8000`，SQL 同时保留 8 秒 hint，source read timeout 为 9～10 秒，总扫描 soft deadline 为 15 秒。源 product 为 `utf8mb4_unicode_ci`：SQL 保留可索引的 `s.product=%s` 前置谓词，同时追加 `BINARY s.product=BINARY %s` 实现精确大小写语义；禁止无界 `DISTINCT`/聚合。按产品返回的原始候选累计超过 20000 时立即失败，不允许先堆积后去重。

### 4.3 优化师

- `optimizer_id` 指 `admin_users.id`，不能与后台 session `user_id` 混用。
- 普通用户按 active `admin_user_group` 逐层使用精确 user ID、email、name 解析；只有唯一结果才可继续，否则返回 `optimizer_identity_unresolved` 或 `optimizer_identity_ambiguous`。
- 普通用户创建规则时优化师由服务端锁定为本人；伪造其他值返回 `optimizer_forbidden`。
- admin 可选择任一 active optimizer，创建人和目标优化师分别审计。
- 普通用户只能读取本人 optimizer 范围的规则和日志；同 optimizer 的其他创建人规则可读但不可改。

### 4.4 账户与时区

- 页面不存在账户、账户池、手工账号控件。
- API 任意嵌套层级出现 account/account-pool 范围字段均返回 `account_scope_forbidden`。
- 账户 ID 仅是候选对象身份的一部分，由源表发现，不能由用户配置。
- 主候选聚合 SQL 无论是否设置时区都不得 JOIN/派生聚合 `ads_accounts_setting`；`account_timezones=[]` 表示不限制，并且 discover 连账户设置表的列/索引 schema probe 都不得执行。
- 仅设置时区范围时，先收齐主查询候选账户，再由 normalized account 生成 bare/`act_` 两种 raw 变体，通过固定 `platform_id=%s AND account_id IN (...)` 绑定参数、`FORCE INDEX(paa)` 分块补查 `account_id,time_zone`。单次 discover 内对跨产品重复账户去重缓存；候选账户最多 5000、每块 raw 变体最多 200、每块返回最多 5000 行，并共用 15 秒 soft deadline。
- 账户号只移除一次前导 `act_`；重复且相同的非空时区合并，多个 distinct 时区返回 `ambiguous_account_timezone`，无有效时区返回 `missing_account_timezone`。任一分块查询失败或截断时整个 discover 失败，不能返回或持久化部分 Preview。
- 本期没有计划调度器，因此时区仅用于范围过滤；账户本地时间的定时执行属于后续发布。

## 5. 对象、动作与运行模式

- 规则组 `object_level` 必须显式选择：`campaign | adset | ad`。
- 每条规则的 `action` 只有 `pause | copy`。
- `observe` 是运行模式，不是动作。
- 新建规则组服务端强制 `enabled=false, run_mode=observe`。
- 手动 Preview 对三层对象可用，产生 `would_pause`/`would_copy` 目标和审计，Token/Graph/Meta 写入计数为 0。
- 同一对象同时命中 pause/copy 时 pause 优先；其余规则记录 `shadowed_by_rule`。
- 启用接口本期被 `runner_scheduler_not_configured`、`live_pause_disabled` 或 `copy_persistence_not_configured` 阻断。

## 6. 条件与候选能力

### 6.1 当前可筛字段

字段目录由 `/meta` 下发，服务端再次校验 `filterable` 和 `previewable`。当前 `ads_custom_source_insight` 可可靠提供：

- 身份：对象 ID、Campaign/Ad Set/Ad ID；
- 产品与剧目：product、`series_code`、APP/APP ID、系统类型；
- 国家与语言：country、country group、language、drama language；
- 发布/素材：最近自动发布日、最近资源创建时间、最近消耗时间；Ad 层资源 ID/名称、source ID、W2A page、素材/任务分类等；
- 效果：spend、impressions、clicks、installs、purchase、revenue、retention、events、ATC、delivery、AF 和广告变现指标；
- 计算指标：CTR、CPM、CPC、CPI、Purchase CPA、ROAS、AF ROAS、广告变现 ROAS。

范围估算固定使用 `identity_only` 主查询投影，只查询对象身份和父级唯一性；用户明确设置时区时，再执行独立的候选账户时区补查。手动 Preview 才由已保存规则条件、Top N 排序字段以及 Copy 的实际 CPI 预算依赖推导 `rule_fields`，只聚合所需原始指标/上下文字段。客户端不能提交 SQL 投影字段，未知字段失败关闭。

`content_id` 不存在于当前源表，目录中必须显示为不可筛。指定剧使用 `series_code`；最近 X 天使用发布/资源时间字段的相对天数操作符。

### 6.2 Roadmap 字段

对象名称、Meta 配置/有效状态、预算、bid control、Campaign objective/CBO、Ad Set optimization goal/billing event、Ad creative ID 当前仅作为 roadmap metadata，`filterable=false, previewable=false, live_ready=false`，UI 不允许选，服务端绕过请求返回 `field_not_supported`。

### 6.3 条件与选择

- 数值：`gt/gte/lt/lte/eq/ne/between/exists/not_exists`。
- 枚举：`eq/ne/in/not_in/exists/not_exists`。
- 文本：`eq/ne/contains/not_contains/starts_with/exists/not_exists`。
- 时间：`before/after/between/within_last_days/older_than_days/exists/not_exists`。
- 规则支持 AND/OR、显式 `metric_window_days`（1～31）和稳定 priority/rule ID 决胜。
- 候选模式：全部、每账户 Top N、每产品 Top N、全范围 Top N；Top N 必须显式提供数值字段和升/降序，对象 ID 最终稳定排序。
- undefined ratio 不命中条件，也不能在升序 Top N 中被当作最佳值。

## 7. 复制、计划和额度配置

复制规则可以保存并在 observe 中形成不可变目标参数，但不会调用 Meta：

- Campaign carrier：`deep_copy_campaign`；
- Ad Set carrier：`same_campaign | new_campaign`；
- Ad carrier：`same_adset | isolated_adset | isolated_campaign`；
- 预算公式：实际 CPI × 倍数、固定目标 CPI × 倍数、来源预算 × 比例；
- ROAS 调整：提高/降低百分比；
- 可选复制冷却天数与单规则日额度。

计划支持固定时间或间隔、可选允许开始/结束；组级额度支持日额度、用户日额度、对象冷却天数。以上字段本期仅保存和回显，runner 未连接，不能据此声称已自动调度或已执行额度扣减。

所有名称、说明、时间、间隔、天数、阈值、Top N、预算倍数和百分比输入初始为空；示例只存在于 placeholder，不进入 payload。

## 8. 数据源与唯一性

- 候选源：`kunlunads_dev.ads_custom_source_insight`，Facebook 为 `platform=0`。
- 时区源：`kunlunads_dev.ads_accounts_setting(platform_id=0)`；只允许候选账户两段式补查并使用已核实的 `paa(platform_id,account_id,account_type)` 前缀，schema/index 漂移失败关闭。生产数据已核实 `platform_id=1` 是 Google 账号格式，不得混用。
- 优化师源：active `admin_user_group` 联 `admin_users`。
- 对象身份：`(channel, normalized_ad_account_id, object_level, object_id)`。
- 同一对象在所选窗口跨产品或跨优化师时返回/记录 `ambiguous_object_scope`，不能猜归属。
- 当前源表没有 Campaign/Ad Set/Ad 名称列和 `content_id`；不得从名称推断产品或剧目。

## 9. 持久化与数据盘

配置和审计只写 `ads_ai` 的八张表：

1. `ad_control_v3_product_catalog`
2. `ad_control_v3_rule_group`
3. `ad_control_v3_rule_group_product`
4. `ad_control_v3_preview`
5. `ad_control_v3_preview_target`
6. `ad_control_v3_execution`
7. `ad_control_v3_execution_target`
8. `ad_control_v3_runner_event`

四处 V3 `product_value/canonical_product` 字段显式使用 `utf8mb4_bin`，避免配置侧大小写折叠。手动试算在一个 MySQL 事务中写 Preview、Preview targets、observe execution、execution targets，并以 compare-and-set 更新规则组 Preview 指针。运行时 reader 固定 63350，writer 固定 63353，数据库固定 `ads_ai`，表名固定白名单；源库只读。

本期不创建、不写 copied `created_data`、lineage 或 intent，也不修改源 `created_data`。

生产数据根固定 `/mnt/data-disk/ai-ad-control-v3`。`SafeDataRoot` 要求独立设备、绝对路径、无符号链接、至少 1 GiB 余量；创建 `snapshots/logs/run/spool/backups/tmp/exports/cache` 子目录。快照为确定性 gzip JSON，原子替换，目录 `0700`、文件 `0600`，数据库只保存相对路径、SHA-256 和大小。读取前复核路径与 hash。

快照清理器本期未实现。数据库写失败可能留下未引用快照，必须保留审计并由后续受审清理器处理，不能人工批量删除。

## 10. API 与安全

- 前缀：`/api/ad-control/v3`，命中后才懒加载 V3；旧请求不会 import V3。
- 页面/资产：cookie + module；写请求：same-origin + JSON，body 上限 2 MiB。
- 规则组支持 CRUD、duplicate、scope estimate、manual preview、disable/emergency stop；enable 本期锁定。
- 列表和执行日志服务端分页；执行详情读取并校验快照。
- 所有 mutation 按创建人授权；普通用户的 optimizer 可见范围由服务端强制。
- 未知字段、未知层级、未知渠道、越权、过期 Preview、数据库/数据盘失败均 fail closed。

## 11. 验收标准

- V3 本地单元/集成测试 139/139 通过，Python 3.9、JavaScript 语法、动态路由、权限、repository、规则引擎、字段投影/查询熔断、两段式时区补查、数据盘、product 精确匹配、主/导航 exact-source 部署器均有自动化证据。
- 本地 Playwright 在 1440 和 390 视口验证两页：中文无乱码、无页面级横向溢出、控制台 0 error/0 warning，并明确展示“仅草稿 + 手动试算/启用锁定”。
- 生产部署、八表 DDL/seed、真实 reader/writer、真实登录、真实三层 observe 和 V2 前后回归必须另行取证；未完成前不得标记上线完成。
- 生产 observe 必须证明 Token lookup、Graph GET/POST/copy 和 Meta 写均为 0。
- 系统盘不产生 V3 快照、运行日志、配置或备份。
- scheduler/enable/live pause/live copy/TT/created_data 写入/清理器保持未发布并失败关闭。

## 12. 回滚边界

- 代码发布使用 exact-source overlay 和数据盘 checkpoint；生产漂移立即停止。
- 空表且从未写入真实数据时才可按审核 SQL 删除；产生真实配置/审计后不删表。
- 代码回滚不删除快照和审计数据。
- 本期没有 Meta 写，因此不存在由 V3 造成的 Meta 状态回滚；未来一旦开放，数据库回滚也不能撤销 Meta 对象状态。
- V2 SQLite、cron 和 runner 不属于 V3 回滚对象。

## 13. 变更记录

- 2026-07-16：创建 V3 并行需求，完成生产只读数据源、身份、Nginx 和数据盘核实。
- 2026-07-16：按冻结实现收口为“FB 动态两页 + 产品多选/优化师/三层 + 手动 observe”；明确 scheduler、enable、live、TT、created_data 和清理器未发布。
