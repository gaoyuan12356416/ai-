# 012.x-post-material-pool 测试用例

## 测试范围

覆盖素材池管理、FIFO、Dramawave 与合规校验、跨表永久排重、三条成组、状态转换、快速导航/daily 鉴权、页面安全、迁移和既有 X 发布回归。

## 测试数据

- 使用临时 SQLite，不读取或修改生产账本。
- MySQL selector 使用 fake read-only connection 和参数化行，不访问生产数据。
- X upload/Create Post 使用 mock client，不使用真实 OAuth Token、不创建真实 Post。
- 日期固定为 `run_date=2026-07-23`、`source_date=2026-07-22`。

## 用例列表

| 编号 | 场景 | 关键步骤 | 预期 |
| --- | --- | --- | --- |
| TC-001 | 批量入池、即时校验与规范化 | 添加 `00101,102,103` 并传一一对应 selector 结果 | 三条及校验状态原子写入，`00101` 规范为 `101`，顺序稳定 |
| TC-002 | 同批重复 | 一次提交 `101,00101,102` | 重复输入计为跳过 1 条，101/102 各写入一次 |
| TC-003 | 池内重复 | 已有 101 后批量添加 `101,102` | 跳过 101，正常写入 102，响应分类计数准确 |
| TC-004 | queue 先存在 | legacy/canary queue 已有 101，再批量添加 `101,102` | 跳过 101，正常写入 102，历史素材不可重新入池 |
| TC-005 | 池先存在再走非池 queue | 先入池 101，再 enqueue 相同 key 且不带 pool ID | fail closed，池不可删除且不显示 available |
| TC-006 | FIFO | 混排传入不同 created_at/id | 选择按 `created_at,id` 正序 |
| TC-007 | 不使用 insight/spend | 执行 manual selector 并检查 SQL | 无 `ads_custom_source_insight`；spend=0 |
| TC-008 | Dramawave 产品门禁 | 同结构素材分别为 Dramawave/其他产品 | 仅精确 Dramawave 通过 |
| TC-009 | 素材基本资格 | 缺失、非视频、删除、时长越界、HTTP URL | 分项安全拒绝，继续扫描 |
| TC-010 | 违规记录 | 四类违规计数逐一设为非 0 | 均允许 X 候选，原值写入 queue 作为审计证据 |
| TC-011 | 内容标签 | source tag、resource tag、drama label 分别命中色情、裸露、暴力等词 | 均允许 X 候选，`dangerous_tag_count` 记录命中数 |
| TC-012 | 剧映射 | 缺失、不完整、跨语言、多个不等价映射 | fail closed；规范等价重复允许 |
| TC-013 | MySQL 查询异常 | fake connection 抛查询错误 | 整批中止，不降级为单素材拒绝 |
| TC-014 | 校验结果回写 | 对未占用素材写 error，再查询 | 主状态 unpublished，派生 validation_failed |
| TC-015 | available summary 口径 | 同时存在普通 available、历史违规/内容标签错误码与真正 validation_failed | 历史合规错误码按 available 统计，发布数据错误仍为 validation_failed |
| TC-016 | 空池/不足三条 | 返回 0、1、2 条或仅 2 条合规 | 记录 failed_preflight；queue/Post 均为 0 |
| TC-017 | 媒体补位 | 前一素材下载/ffprobe 失败，后续合格 | 删除失败临时文件，后续候选补足三条 |
| TC-018 | 媒体仍不足三条 | 扫描后只有 2 条媒体通过 | 不创建计划、不发布 |
| TC-019 | 计划原子与 FIFO 快照 | 逆序、快照变化、已占用、并发创建 | 全部回滚；正确三条一次提交 |
| TC-020 | 全局永久排重 | 池 ID 或 material key 已绑定 queue | 后续 available/add/delete/plan 均不能复用 |
| TC-021 | known failure | queue 发布明确失败 | 池主状态 unpublished，派生 failed，不再选择 |
| TC-022 | unknown | transport unknown 或残留 post_creating | 池主状态 unpublished，派生 needs_review，不重试 |
| TC-023 | 成功态 | media + Create Post + 本地事务成功 | queue/log/pool 同步 published，记录 preview/published_at |
| TC-024 | 成功重放 | 相同已发布 log/post ID 重放 | 不再次写 X；可自愈池 published |
| TC-025 | 删除门禁 | 删除 available、occupied、published | 仅 available 成功 |
| TC-026 | 素材池 API 鉴权 | 导航 `adminOnly` 开/关、模块权限有/无、菜单启用/禁用、API Token/跨源请求 | 仅符合 `xPostMaterialPool` 快速导航配置的 Cookie 用户可访问；写请求仍须同源 |
| TC-027 | daily bearer 范围 | 调用 available/check 与 add/query/delete | 前者允许，管理路由 403 |
| TC-028 | 查询参数与脱敏 | 非法枚举、未知参数、错误文本、敏感词 | 400 或脱敏安全 DTO，no-store |
| TC-029 | 页面 DOM 安全 | 恶意素材名/错误/URL | 使用 textContent/replaceChildren；素材预览仅安全 HTTPS 直链并带 noopener/noreferrer，Post 预览仅 x.com allowlist |
| TC-030 | legacy 迁移 | 旧库副本执行 ensure_storage 两次 | 幂等新增表/列/索引/触发器；冲突 fail closed |
| TC-031 | 两级扫描窗口 | 前 50 条不合规、51 至 1000 内有安全素材 | 原始池读取 scan limit，不能被候选上限 50 提前截断；最老 1000 条内仍不足三条则整批不发 |
| TC-032 | 既有发布回归 | X service/account/owner/daily 全套测试 | 旧 canary、OAuth、短链、unknown、限流语义不变 |
| TC-033 | 检查回写分批 | 生成 205 条互异 pool check | 调用 Sidecar 三次，批量严格为 100/100/5，无记录因超限整批丢失 |
| TC-034 | 素材源文件预览 | 管理员查询含合规/不合规/不存在素材的池列表；再测试 HTTP、凭据、端口和 CRLF URL | 有安全源 URL的合规/不合规项均返回精确 `material_preview_url`；不存在/不安全项为空；页面直接打开且发布状态 0 写入 |
| TC-035 | 入池即时 X 校验 | 混合提交合规、不合规、不存在素材；模拟 selector/数据库异常 | 复用正式 selector；合规项立即 available，其余立即 validation_failed/“不可用”；异常 fail closed，不出现待校验可用窗口 |
| TC-036 | Sidecar 入池校验合同 | 省略、错配、冲突重复或不完整 `validation_checks` | 省略时统一 pending/不可用；非法集合整批 400、0 写入；相同重复检查可去重，合法集合与池记录原子写入 |
| TC-037 | 页面导航授权一致性 | 普通用户有 `x_accounts` 且 `adminOnly=false`，再覆盖 true/禁用/配置读取失败 | false 时页面可加载；true、禁用或读取失败时 fail closed；页面不再写死 `user.is_admin` |
| TC-038 | 100 条批量入池 | 一次提交 100 个互异全新 ID | 100 条全部写入，不触发上限误判 |
| TC-039 | 10 条含 1 条池内重复 | 已有 1 条后提交该条加 9 条全新 ID | 新增 9、跳过 1；前端提示新增/跳过分类，不整批失败 |
| TC-040 | 短剧可投放时间 | 构造 Dramawave 多端过去/等于/未来/缺失/非法 `deploy_time`，并把当前时间推进到边界 | 多端取最晚值；未来时间以 `drama_not_yet_deliverable` 跳过并继续 FIFO 扫描；等于或超过边界自动恢复；缺失/非法 fail closed |

## 自动化映射

- `scripts/test_x_post_material_pool.py`：TC-001 至 TC-005、TC-014 至 TC-025、TC-036、TC-038、TC-039 的账本核心。
- `scripts/test_x_post_material_pool_selector.py`：TC-006 至 TC-013、TC-040。
- `scripts/test_x_post_daily.py`：TC-016 至 TC-019、TC-027、runner 失败语义。
- `scripts/test_x_post_ledger.py`：TC-019、TC-020、TC-024、TC-030。
- `scripts/test_x_accounts.py`：Sidecar 路由、daily bearer 和发布状态回归。
- `scripts/test_x_accounts_app_contract.py`：TC-026、TC-028、TC-029、TC-034、TC-035、TC-037。
- `scripts/test_x_posts.py` / `scripts/test_x_account_owner_backfill.py`：TC-032。

## 生产验收边界

- 部署前只允许只读数据抽样、SQLite 副本迁移和 mock/offline 测试。
- 不以手工执行 daily oneshot 验收真实 X 发布。
- 首个正式批次由确认后的自然 timer 触发，并在 AI 后台核对三条 queue/log/pool 状态。
