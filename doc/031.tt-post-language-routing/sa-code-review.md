# SA 代码评审

## 结论

代码评审通过，可进入生产副本迁移演练和部署准备。自动/手动边界、语言 FIFO、事务冻结、旧值兼容和页面/代理契约均已落地；自动化验证不包含真实 TikTok 发布。生产部署仍须按 `deploy.md` 独立审批和留证。

## 评审范围

- `features/tt_posts/core.py`
- `features/tt_posts/service.py`
- `app.py`
- `static/tt-account-settings.html`
- `static/tt-post-pool.html`
- TT Post Core/Service/页面/代理测试

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | P0 | 自动 claim | 若只过滤原 `account_id`，并未实现动态跨池 | 自动触发按规范语言查询全池；手动才保留账号过滤 | 已关闭 |
| CR-02 | P0 | claim 事务 | 查询和 reserve 分离会重复领料 | 同一 `BEGIN IMMEDIATE` 完成查询、run、pool 更新 | 已关闭 |
| CR-03 | P0 | 无匹配 | fallback 或标记消费会误发/丢料 | 保持 `available`，不创建 queue | 已关闭 |
| CR-04 | P0 | 恢复 | 已有 run/queue 若按新语言重选会破坏幂等 | 恢复冻结 pool/run；publish_id/unknown 只核对 | 已关闭 |
| CR-05 | P0 | 手动/canary | 共用自动跨池查询可能自动换号 | 明确 `trigger_type` 分支和精确 canary 回归 | 已关闭 |
| CR-06 | P1 | AIGC | pool 可能保留入池预分配账号值 | reserve 时写目标账号当前 `is_aigc` | 已关闭 |
| CR-07 | P1 | 语言规范化 | 前后端各自实现可能漂移 | 服务端单一规范函数；UI 只辅助，不作为可信边界 | 已关闭 |
| CR-08 | P1 | 旧客户端 | 缺字段可能写空或被拒绝 | 缺失/空统一 en，并有代理/API 回归 | 已关闭 |
| CR-09 | P1 | 页面表格 | 新列未同步空态 colspan 会错位 | 所有空态 9 列；100% 布局检查 | 已关闭 |
| CR-10 | P1 | 代理审计 | 语言未入审计会缺少变更证据 | 单条/批量摘要加入 `drama_language` | 已关闭 |
| CR-11 | P1 | 规范值长度 | 原始值先限长会漏掉 Unicode casefold 扩长 | 规范化后再校验 1–32，所有格式/超长统一错误码 | 已关闭 |
| CR-12 | P0 | active canary | 其他同语言账号可能自动抢走精确测试素材 | 自动 claim 在同一事务排除 active canary pool | 已关闭 |
| CR-13 | P1 | 手动 readiness | 全局语言数量会错误启用精确账号 run-now | 自动/手动可用数分离，按钮只使用精确账号计数 | 已关闭 |
| CR-14 | P1 | 大池查询 | Python 全池扫描会延长 SQLite 写锁 | 持久规范路由键并建立语言 FIFO 复合索引 | 已关闭 |
| CR-15 | P1 | 历史脏语言 | 一条非法旧值可能阻断后续合法素材 | 迁移时写隔离键，公共列表保留可见且不参与匹配 | 已关闭 |
| CR-16 | P1 | 大池筛选 | 合并前各截断 1000 条会遗漏后续实际账号记录 | 分批取全后合并实际领取账号，再筛选和分页 | 已关闭 |
| CR-17 | P1 | canary 按钮 | 活动 canary 且排期启用时按钮可能与 run-now 锁定冲突 | canary 目标账号只以精确 canary readiness 决定按钮 | 已关闭 |
| CR-18 | P1 | 发布状态聚合 | 全量池超过 1000 个素材会触发状态查询上限 | 过滤后按每批至多 1000 个 material_id 聚合并合并结果 | 已关闭 |

## 编译 / 验证结果

- `python scripts/test_tt_account_settings_ui.py`：12/12 通过。
- `python scripts/test_tt_post_pool_ui.py`：36/36 通过。
- `python scripts/test_tt_posts_app_contract.py`：13/13 通过。
- `python -m unittest discover -s scripts -p "test_tt*.py"`：完整 TT Python 回归 372/372 通过，其中 Core 83、Service 130。
- `node scripts/test_tt_drama_bridge.js`：53 项断言通过。
- py_compile 与 `git diff --check`：通过；仅有 Git 的 LF/CRLF 工作树提示，无空白错误。
- 所有测试使用临时数据和 fake 外部依赖，未调用真实 TikTok Post。

## 非阻塞上线检查

- 语言筛选在 `BEGIN IMMEDIATE` 内使用 `(status,routing_language,created_at,id)` 索引首行查询；生产副本仍需记录查询计划、耗时和可用池规模。
- 新列是 additive migration；正式部署前仍须做 SQLite 在线备份、副本双初始化和 `integrity_check`。
- 登录态 100% 缩放与公网页面文件哈希属于部署验收，不由单元测试替代。
