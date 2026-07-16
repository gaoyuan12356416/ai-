# 测试用例

## 1. 测试边界与状态口径

- 本地自动化使用 MemoryRepository/Fake MySQL、Stub insight、Memory/SafeDataRoot 和 route handler，不访问生产、不调用 Meta。
- 本地 Playwright 使用 UTF-8 mock API，验证冻结前端；不等同于生产登录或真实 MySQL。
- “本地通过”表示存在于本次 119/119 自动化证据；“部分通过”表示本地合同已证实但生产部分尚未执行。
- scheduler、enable 成功、live pause、live copy、TT 执行、copied created_data 和快照清理器属于未发布能力，测试目标是门禁，不是成功执行。
- 生产/MySQL/V2 项只有真实证据后才能改为通过。

## 2. 测试数据

| 标识 | 说明 |
| --- | --- |
| `ADMIN_A` | admin，可选择 active optimizer |
| `OPT_A/OPT_B` | 两个不同 optimizer 的普通用户 |
| `OPT_NONE/OPT_AMBIG` | 无映射/多映射登录身份 |
| `DRAMA_A/DRAMA_B` | 精确 FB 短剧产品枚举 |
| `NON_DRAMA/DISABLED` | 未审核/停用产品 |
| `FB_3_LEVELS` | Campaign/Ad Set/Ad、多产品、多日、时区与歧义数据 |
| `GRAPH_SPY` | Token/Graph 调用计数桩；冻结实现不存在外部 mutator |
| `DATA_ROOT_TMP` | 本地非生产测试根，显式放宽“独立设备”检查 |
| `V2_BASELINE` | 旧文件、路由、SQLite、cron、runner、自然 tick 生产基线 |

## 3. 用例清单

### A. 动态页面与 V2 隔离

| 编号 | 场景 | 预期 | 优先级 | 当前状态 |
| --- | --- | --- | --- | --- |
| TC-001 | 动态规则组页 | cookie/module 后返回 200 HTML、no-store、CSP；未登录不渲染 | P0 | 部分通过；生产待验 |
| TC-002 | 动态日志页 | 与规则页相同安全头，仅加载日志视图 | P0 | 部分通过；生产待验 |
| TC-003 | 无 V3 静态 HTML | 仓库无 V3 public HTML，模板仅位于 feature；生产 Nginx root 也应无副本 | P0 | 部分通过；生产待验 |
| TC-004 | 仅两个一级页面 | V3 导航/侧栏只有规则组管理、执行日志 | P0 | 本地自动化和 Playwright 通过 |
| TC-005 | 前进/后退/刷新 | 两动态 URL 可刷新，筛选/编辑状态不产生空白页 | P1 | 待执行（生产浏览器） |
| TC-006 | 登录与模块权限 | cookie/API Token/无模块权限分别得到正确拒绝，零业务泄露 | P0 | 部分通过；生产身份待验 |
| TC-007 | V2 路由零变更 | V3 guard 仅命中新前缀，旧契约不变 | P0 | 部分通过；V2 生产回归待验 |
| TC-008 | V2 存储零变更 | V3 CRUD/Preview 不读写旧 SQLite | P0 | 待执行（V2 生产基线） |
| TC-009 | V2 runner 零影响 | 旧 cron/runner/event key/自然 tick 前后一致 | P0 | 待执行（生产） |
| TC-010 | V3 故障隔离 | ads_ai/数据盘不可用只使 V3 失败，V2 仍正常 | P0 | 待执行（生产） |

### B. 产品、优化师、时区与范围

| 编号 | 场景 | 预期 | 优先级 | 当前状态 |
| --- | --- | --- | --- | --- |
| TC-011 | 产品目录动态下发 | `/meta` 来自 ads_ai active short-drama catalog；seed 精确 15 值 | P0 | 部分通过；生产 seed/readback 待验 |
| TC-012 | 产品多选保存/回显 | 精确值写 relation 表，hash 稳定，无账户字段 | P0 | 本地自动化通过 |
| TC-013 | 非法/别名产品 | 源查询保留索引等值并用 BINARY 精确复核；大小写别名不混入候选，非法目录值零写入 | P0 | 本地自动化通过；生产 EXPLAIN 待验 |
| TC-014 | 停用产品历史规则 | 历史可读；重新保存/试算/启用时不得绕过 active catalog | P1 | 待执行（真实 MySQL 联调） |
| TC-015 | 大表查询边界 | 单产品 query 强制 platform/product/dt/optimizer、`MAX_EXECUTION_TIME(4000)`、索引等值 + BINARY exact 与 `pss`，无无界扫描 | P0 | 部分通过；生产 EXPLAIN 待验 |
| TC-016 | 普通用户锁定本人 | UI 锁定，伪造其他 optimizer 返回 `optimizer_forbidden` | P0 | 本地自动化通过 |
| TC-017 | admin 代建 | 目标 optimizer 必须 active，creator/optimizer 分开审计 | P0 | 本地自动化通过；生产身份待验 |
| TC-018 | optimizer 无映射 | fail closed，不创建规则 | P0 | 本地自动化通过 |
| TC-019 | optimizer 歧义 | 不取第一条，meta/CRUD/Preview 均阻断 | P0 | 本地自动化通过 |
| TC-020 | 规则/日志隔离 | 普通用户只见本人 optimizer；他人创建的同 optimizer 规则只读不可改 | P0 | 本地自动化通过 |
| TC-021 | 产品+optimizer 双约束 | SQL 和结果均限定 FB/精确产品/optimizer；跨产品对象阻断 | P0 | 部分通过；真实数据待验 |
| TC-022 | 页面无账号控件 | DOM/可访问树/payload 无账户和账户池 | P0 | 本地自动化和 Playwright 通过 |
| TC-023 | API 递归拒绝账号字段 | 任意嵌套 account key 返回 `account_scope_forbidden` | P0 | 本地自动化通过 |
| TC-024 | 范围估算不可选账号 | 只返回账户/对象/阻断计数，不形成账户配置 | P1 | 本地自动化通过 |
| TC-025 | 时区留空不限制 | 不加时区过滤，重复同值设置不误阻断 | P0 | 本地自动化通过 |
| TC-026 | 时区精确多选 | 只匹配用户选择的 server enum；不静默猜同义值 | P1 | 本地自动化通过；生产枚举待验 |
| TC-027 | 缺失/冲突时区 | 设置时区后缺失或冲突对象在规则前阻断并记录原因 | P0 | 本地自动化通过 |

### C. 无默认值与表单

| 编号 | 场景 | 预期 | 优先级 | 当前状态 |
| --- | --- | --- | --- | --- |
| TC-028 | 新建业务输入为空 | 名称、时间、阈值、Top N、预算等无 value 默认值 | P0 | 本地自动化和 Playwright 通过 |
| TC-029 | placeholder 不入 payload | 未填写必填项只显示错误，示例不提交 | P0 | 本地自动化通过 |
| TC-030 | 枚举显式选择 | 产品、层级、动作、窗口/选择模式不静默取第一项 | P0 | 本地自动化通过 |
| TC-031 | 服务端安全状态 | create 强制 disabled/observe，UI 明示而非伪装填充值 | P0 | 本地自动化通过 |
| TC-032 | 编辑精确回显 | `0` 与空值不混淆，使用 group_id/config_version | P1 | 本地自动化通过 |
| TC-033 | 未保存离开提示 | 切页/刷新有明确丢弃确认与焦点恢复 | P1 | 待执行（人工浏览器） |
| TC-034 | 隐藏字段清理 | level/action 切换不提交不兼容 Copy 参数 | P0 | 本地自动化通过 |

### D. 三层、字段目录与规则引擎

| 编号 | 场景 | 预期 | 优先级 | 当前状态 |
| --- | --- | --- | --- | --- |
| TC-035 | 三层显式可选 | campaign/adset/ad 均可保存并手动 observe | P0 | 本地自动化通过 |
| TC-036 | 层级能力目录 | 切层后只显示该层可用字段，旧不兼容条件不提交 | P0 | 本地自动化通过 |
| TC-037 | Roadmap 字段门禁 | Meta 名称/状态/预算等 UI 禁选，服务端 `field_not_supported` | P0 | 本地自动化通过 |
| TC-038 | 类型/操作符 | 数值、枚举、文本、时间操作符与 catalog 一致 | P1 | 本地自动化通过 |
| TC-039 | between 边界 | 必须两值，闭区间边界正确 | P1 | 本地自动化通过 |
| TC-040 | exists 与零值 | 0 不当作缺失，null/空按契约判断 | P1 | 本地自动化通过 |
| TC-041 | AND/OR | 命中集合与规则快照一致 | P0 | 本地自动化通过 |
| TC-042 | 指标窗口 | `metric_window_days` 1～31 必填；估算/Preview 发送显式层级和窗口 | P0 | 本地自动化通过 |
| TC-043 | 无原始 JSON 编辑器 | 普通/admin 均只能可视化配置 | P0 | 本地自动化通过 |
| TC-044 | 至少一条有效规则/条件 | 空规则、空条件、未知字段均拒绝 | P0 | 本地自动化通过 |
| TC-045 | 三层聚合与身份 | 多日/多素材聚合，object key 去重，单值上下文歧义阻断 | P0 | 本地自动化通过 |
| TC-046 | pause/copy 冲突 | pause 胜出，其他命中写 `shadowed_by_rule` | P0 | 本地自动化通过 |
| TC-047 | 同动作优先级 | priority 后按 rule ID 稳定决胜 | P1 | 本地自动化通过 |
| TC-048 | 四种选择与排序 | all/account/product/global Top N、升降序和 object ID 稳定 | P0 | 本地自动化通过 |
| TC-049 | 剧目条件 | 指定剧使用 `series_code`；最近剧使用发布/资源时间；`content_id` 不可筛 | P1 | 本地自动化通过 |

### E. 动作、计划配置与安全门

| 编号 | 场景 | 预期 | 优先级 | 当前状态 |
| --- | --- | --- | --- | --- |
| TC-050 | 动作仅 pause/copy | observe 只作为 run mode，未知 action 拒绝 | P0 | 本地自动化通过 |
| TC-051 | 三层 Copy carrier | 各层只接受其合法 carrier，UI 同步变化 | P1 | 本地自动化通过 |
| TC-052 | Copy 数值校验 | 预算/比例/ROAS/冷却/额度为空或越界时拒绝 | P1 | 本地自动化通过 |
| TC-053 | Copy 参数不可变审计 | observe target 保存命中时的完整 copy_parameters；不判断 Meta 兼容性 | P0 | 本地自动化通过 |
| TC-054 | 固定时间仅配置 | HH:MM 校验、空值拒绝；本期不计算 due | P0 | 本地自动化通过 |
| TC-055 | 间隔仅配置 | 正整数、上限 1440；本期不触发 scheduler | P1 | 本地自动化通过 |
| TC-056 | 允许起止窗口仅配置 | 合法 HH:MM 精确保存；跨午夜执行语义留待 scheduler | P1 | 本地自动化通过 |
| TC-057 | 新建安全状态 | 永远 disabled/observe、无旧 Preview | P0 | 本地自动化通过 |
| TC-058 | 更新失效 Preview | version/hash 变化、pointer 清空、历史保留 | P0 | 本地自动化通过 |
| TC-059 | 三层手动 observe 零外部写 | adapter 无 external mutator，summary `meta_write_count=0`，写快照/审计 | P0 | 本地自动化通过；生产零调用待验 |
| TC-060 | live pause 门禁 | 返回 `live_pause_disabled`，零 Token/Graph | P0 | 本地自动化通过 |
| TC-061 | live copy 门禁 | 返回 `copy_persistence_not_configured`，零 adapter copy | P0 | 本地自动化通过 |
| TC-062 | TikTok 门禁 | meta disabled；保存/扫描均 `channel_not_enabled` | P0 | 本地自动化通过 |
| TC-063 | stale Preview | 更新/急停/过期/hash 不同后不可 enable | P0 | 本地自动化通过 |
| TC-064 | enable 本期锁定 | observe 为 scheduler 未配置，live 为 mutation 未发布；confirm 不绕过 | P0 | 本地自动化通过 |
| TC-065 | 急停 | 原子 disabled+emergency+clear preview；恢复需新 Preview，enable 仍锁定 | P0 | 本地自动化通过 |
| TC-066 | ads_ai 事务失败 | Preview/Execution bundle 回滚，无可执行 pointer、无外部写 | P0 | 本地自动化通过 |
| TC-067 | 前端重复点击 | mutation single-flight，不因双击发两次；后端未声明通用 idempotency key | P0 | 本地自动化通过 |
| TC-068 | 最终 CAS | enable SQL 同时校验 version/hash/Preview/expiry/enabled 前提 | P0 | 本地自动化通过 |

### F. ads_ai、数据盘与日志

| 编号 | 场景 | 预期 | 优先级 | 当前状态 |
| --- | --- | --- | --- | --- |
| TC-069 | 八表边界 | SQL 只创建八张 `ads_ai.ad_control_v3_*`，四处 product 字段为 `utf8mb4_bin`，无 copied created_data | P0 | 部分通过；生产 DDL 待验 |
| TC-070 | 数据盘门禁 | 缺失/系统盘/应用目录/symlink 拒绝；生产必须独立 mount | P0 | 部分通过；生产 mount 待验 |
| TC-071 | 原子快照 | gzip JSON、raw/gzip 上限、0600/0700、fsync+replace、低空间拒绝 | P0 | 本地自动化通过 |
| TC-072 | 相对路径/hash | `..`/逃逸/损坏 hash 均拒绝读取 | P0 | 本地自动化通过 |
| TC-073 | 快照清理 | 本期无清理器，不得自动/人工批删；后续需引用感知实现 | P1 | 阻塞（未发布） |
| TC-074 | V3 事件日志 | 列表只展示 V3 manual observe 事件，计数不伪造；无业务日合并承诺 | P1 | 本地自动化和 Playwright 通过 |
| TC-075 | 日志组合筛选 | 产品多选、optimizer、层级、动作、模式、状态、trigger、object ID | P0 | 本地自动化通过；生产数据待验 |
| TC-076 | 详情懒加载 | 列表不读快照；详情验证 hash 并返回 header/target count | P1 | 本地自动化通过 |
| TC-077 | 服务端分页 | 规则/日志 page/page_size 上限，稳定排序，不全量渲染 | P0 | 本地自动化通过 |
| TC-078 | 日志安全 | XSS 文本转义，未知计数不伪造；生产日志不得出现 secret | P0 | 部分通过；生产日志审计待验 |

### G. 响应式、可访问性和健壮性

| 编号 | 场景 | 预期 | 优先级 | 当前状态 |
| --- | --- | --- | --- | --- |
| TC-079 | 1440/390 响应式 | 两页无页面级横向溢出，关键操作可达，中文完整 | P1 | 本地 Playwright 通过 |
| TC-080 | 200% 缩放 | 主要流程不丢控件、错误或焦点 | P1 | 待执行（人工浏览器） |
| TC-081 | 全键盘 | 导航、产品、条件、保存、日志均可达，无键盘陷阱 | P1 | 待执行（人工浏览器） |
| TC-082 | label/错误关联 | 基础语义结构存在；屏幕阅读器/首错焦点另行抽查 | P1 | 部分通过；人工无障碍待验 |
| TC-083 | 非颜色唯一/对比度 | 状态带文字；WCAG AA 需实测 | P1 | 部分通过；对比度待验 |
| TC-084 | 确认焦点 | 删除/急停确认的 Tab/Escape/焦点恢复 | P1 | 待执行（人工浏览器） |
| TC-085 | reduced motion/触控 | CSS motion guard 存在；主要触控目标不依赖 hover | P2 | 本地自动化通过；真机待验 |
| TC-086 | XSS/长文本/UTF-8 | bootstrap 与运行时转义；中文无乱码；console 0 error/0 warning | P0 | 本地自动化和 Playwright 通过 |
| TC-087 | 请求竞态 | 写操作 single-flight；旧 list/estimate 响应不覆盖新状态 | P1 | 本地自动化通过；生产延迟待验 |
| TC-088 | 网络/会话变化 | loading/empty/error 可见，不误报成功；生产会话过期/权限回收待验 | P0 | 部分通过；生产待验 |

## 4. 已取得证据

- `python -m unittest discover -s tests -p "test_ad_control_v3*.py" -v`：119/119；其中 product/安全相关子集 54/54、navigation 发布链 13/13。
- Playwright 冻结代码：1440/390 规则页和日志页；`scrollWidth == viewport`；console 0 Errors / 0 Warnings；页面明确“调度器未发布/仅草稿+手动试算/启用锁定”。
- 截图目录：`D:\codex\tmp\ad-control-v3-ui-final`。

## 5. 发布通过准则

- 本地 P0 自动化全部通过只是提交门禁，不是生产验收。
- 生产必须完成八表 DDL/seed/readback、真实身份/权限、三层 manual observe、数据盘和 V2 前后回归。
- 三层生产 observe 都必须有 Token/Graph/Meta 写 0 的独立证据。
- 任一 P0 生产项未执行或失败，测试报告保持“不可放量”。
