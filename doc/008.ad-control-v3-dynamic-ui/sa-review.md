# SA 需求与方案评审

## 结论

需求与实现边界已收口，可进入 GitHub 精确提交、生产前 staging 和数据库审核；**不等于允许启用调度或 Meta 写入**。

本期发布对象仅为 Facebook 动态两页、规则配置、范围估算和手动 observe。旧 V2 继续独立运行。scheduler、规则 enable、live pause、live copy、TikTok、复制结果 `created_data/lineage/intent` 和快照清理器均未发布且失败关闭。

## 评审基线

- V2 冻结 Git 基线：`2b52bc8d06b8a36a473dad8916012570ee28c15b`。
- V3 worktree：`D:\codex\ai-drama-material-service-ad-control-v3`。
- V3 分支：`codex/ad-control-v3-dynamic-ui`。
- 生产只读核实：现网 `app.py` SHA-256 与基线 commit 对应版本一致；最终发布前仍必须重新读取。
- 生产数据盘：`/mnt/data-disk`，V3 根目录固定 `/mnt/data-disk/ai-ad-control-v3`。
- MySQL：业务/ads_ai reader 使用 63350，ads_ai writer 使用 63353；生产 DDL 尚未执行。
- 源表：`kunlunads_dev.ads_custom_source_insight`，FB `platform=0`，`optimizer=admin_users.id`。

## 关键决策

1. 新旧系统平行：V3 路由前缀、feature 包、表、数据目录和 runner 文件独立；旧路径不导入 V3。
2. 动态两页：后端模板和 allowlist asset 路由，不新增 V3 静态 HTML。
3. 范围由产品多选 + optimizer + 可选时区确定；账户只作为服务端发现的对象身份。
4. 普通用户 optimizer 必须唯一映射且锁定本人；admin 才能代选。
5. 当前只从受限 `ads_custom_source_insight` 查询可验证字段；Meta 名称、状态、预算、出价和 Creative 等字段保持 roadmap、不可筛。
6. `content_id` 源列不存在；指定剧使用 `series_code`，最近剧使用发布/资源时间条件。
7. 手动 Preview 是唯一已连接的运行路径，必须为 Meta 写 0。
8. 八张 `ads_ai.ad_control_v3_*` 表保存配置、Preview 与 execution；大快照和备份走数据盘。

## 问题清单

| 编号 | 级别 | 问题 | 处理结果 | 状态 |
| --- | --- | --- | --- | --- |
| SA-001 | P0 | 新 V3 不能污染 V2 路由/SQLite/runner | `app.py` 只添加命中 V3 前缀后的 lazy dispatcher；其余 V3 文件独立 | 本地评审通过，生产差异待验 |
| SA-002 | P0 | 产品/优化师只在 UI 过滤会越权 | service、schema、repository 与 Facebook query 均强制范围；账户字段递归拒绝 | 本地自动化通过 |
| SA-003 | P0 | session user 与 optimizer 身份域不同 | active user-group 分层精确解析，结果必须唯一；admin 枚举独立 | 本地自动化通过，生产真实身份待验 |
| SA-004 | P0 | 大表无界查询与 product collation | 每个产品独立查询，强制 data_source 0/6、platform/product/dt/optimizer 和 dpdo；scope 身份投影、Preview 规则字段投影；raw 候选累计硬限 20000；8s session/hint、9～10s socket、15s 总扫描 soft deadline；保留索引等值并加 BINARY exact，schema/index 漂移失败关闭 | SQL/投影/超时单测通过，生产 EXPLAIN 待验 |
| SA-005 | P0 | 动态页面权限与 XSS | cookie/module、same-origin JSON、2 MiB、CSP、bootstrap JSON 转义、asset allowlist | 本地自动化/Playwright 通过，生产认证待验 |
| SA-006 | P0 | MySQL 读写角色混用 | reader 63350、writer 63353、固定 host/database/table allowlist、显式事务 | 自动化边界通过，真实账号/复制延迟待验 |
| SA-007 | P0 | Preview/Execution 部分提交 | Preview、两类 targets、observe execution 与规则组 pointer 在同一 writer 事务提交 | repository 自动化通过 |
| SA-008 | P0 | 未发布能力误启用 | meta 标记 scheduler/live/copy/TT disabled；runner 即使开启两个 env 仍返回 scheduler 未配置 | 本地自动化通过 |
| SA-009 | P1 | 数据盘误落系统盘/路径逃逸 | SafeDataRoot 验证绝对路径、独立设备、symlink、余量、权限、大小、相对路径和 hash | 本地自动化通过，生产 mount 待验 |
| SA-010 | P1 | 字段目录超报能力 | 仅 insight 可可靠字段为 previewable；Meta 状态/预算等标记不可筛 | 本地自动化通过 |
| SA-011 | P1 | Copy 参数跨层污染 | Campaign/Ad Set/Ad 分别使用层级合法 carrier；UI 切换时清理隐藏值，服务端再次校验 | 本地自动化通过 |
| SA-012 | P1 | 快照写成功而 DB 事务失败会留下孤儿 | 不引用、不执行；本期不发布清理器，保留为后续受审补偿/清理任务 | 接受的遗留风险 |
| SA-013 | P1 | 产品停用后历史规则的 Preview/enable 复核 | 当前保存时校验 active product；生产停用后的全链路复核尚需联调 | 生产联调待验 |
| SA-014 | P1 | 计划/额度配置易被误认为已执行 | UI 和文档明确“仅保存 + 手动试算”，启用锁定，runner 未连接 | 已收口 |
| SA-015 | P0 | settings 派生表 JOIN 使三层候选聚合超时 | 主聚合永不 JOIN settings；仅非空时区筛选对候选账户生成 bare/act_ 变体，以绑定参数、`platform_id=0` 和 `FORCE INDEX(paa)` 分块补查；请求内去重缓存，5000 账户/200 变体每块/5000 行每块为不可放大的硬限，共用 15s soft deadline；空时区连 settings schema probe 都跳过；缺失/冲突/任一分块查询失败均失败关闭且不写部分 Preview | 59/59 core 与 139/139 V3 通过；生产三层 EXPLAIN/实查待验 |

## 生产前强制门禁

- 139/139 本地 V3 测试必须在最终 commit 再跑一次并保存输出。
- GitHub push 后服务器只 fetch 精确 commit；exact-source deployer `--check` 必须为 `would_change` 或 `unchanged`。
- DDL/seed 只允许落 `ads_ai` 八表，先保存 schema 基线并在 63350 回读校验。
- V3 配置文件、快照、cache 和备份必须位于 `/mnt/data-disk/ai-ad-control-v3`。
- 真实普通用户、admin、无权限用户分别验证页面/API 和 optimizer 隔离。
- 三层各做一次小范围手动 observe；记录 Token/Graph spy 或等价零外部调用证据。
- V2 文件、路由、SQLite、cron、runner 和自然 tick 做发布前后对比。
- 不创建/启用 V3 timer，不开放任何 live/copy/TT 开关。

## 评审结论说明

当前结论是“本地实现可进入生产前审核”，不是“生产验收通过”。生产 MySQL、服务部署、V2 回归和线上浏览器均在 `test-report.md` 标为待执行；未取得证据前不得修改结论。

## 变更记录

- 2026-07-16：完成初次有条件评审，要求规范化存储、身份唯一映射和数据盘隔离。
- 2026-07-16：基于冻结实现复审，收窄本期为手动 observe，补充未发布能力和遗留风险。
- 2026-07-16：settings 全表派生 JOIN 生产只读验证超时后，改为候选账户两段式 `paa` 索引补查并补齐失败关闭/上限回归。
