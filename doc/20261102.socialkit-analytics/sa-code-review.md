# SA 代码评审

## 结论

2026-11-02 独立静态审查及本地用例审查已完成核心授权、查询和采集边界检查。发现的授权放宽、未发布快照读取和迁移名称不一致问题已修复；部署与真实 14 天验收由部署记录和测试报告单独给出，本文件不声称已通过线上验收。

## 评审范围

- `ops/socialkit-analytics/integrate.py`、`install.py`：按当前线上文本窄补丁，不用旧 Git 基线覆盖整份 app；迁移只允许本功能三表，环境文件不输出凭据。
- `features/socialkit_analytics/navigation.py` 及 `static/quick-nav.js` 的本功能迁移：独立模块授权，旧组/项组合 ACL 保持。
- `collector.py`、`storage.py`、`refresh.py`、`migration.sql`：精确短链、平台换码、源只读事务、SQL Gate、整数分控制、版本发布及有限清理。
- `query.py`、`routes.py`：本地只读快照、过滤白名单与参数化 SQL、已发布版本约束、CSV 同版本和公式字符串转义。
- 查询技能入口：仅调用已保存 SSH helper 和查询模块，不含凭据，也不启动采集。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SK-R1 | P1 | navigation.py / integrate.py | 默认迁移直接删除组 module 会使带不同 item module 的旧项失去原组权限要求 | 自定义组合保留旧组，新报表放独立 sibling；测试前后权限 | 已修复，后端测试及实际 JS 迁移对照通过 |
| SK-R2 | P1 | query.py snapshot | 任意存在的 SQLite 文件可按 version 打开，包含完成构建但尚未发布的孤立快照 | 仅 current/previous 清单登记版本可读 | 已修复，孤立快照回归通过 |
| SK-R3 | P1 | migration.sql / storage.py | 迁移表名 report_runs 与批准 refresh_runs 不一致，另含安装器拒绝的 USE 语句 | 统一三表名称并移除 USE | 已修复，复核 SQL 与运行时代码一致 |
| SK-R4 | P2 | install.py rollback | 文件哈希预检拒绝回滚前已停用 timer，可能意外取消原正常调度 | 全部回滚预检通过后再停用 timer | 已修复，拒绝回滚无命令/文件副作用测试通过 |
| SK-R5 | P2 | collector.py dimensions | 启动时全量历史任务 GROUP BY 取剧名，与按需维度查询目标不符 | 本轮引用 app/content 惰性查名并缓存 | 已修复，复合键/缓存用例通过 |
| SK-R6 | P1 | migration.sql / install.py | COMMENT 字符串中分号被安装器拆成多段，迁移解析失败 | 注释内改逗号并直接模拟执行当前 SQL | 已修复，恰好三表 DDL 模拟通过 |
| SK-R7 | P2 | query.py / 页面 | 未归属提示只按收入非零，忽略零收入但有点击/安装的未知键 | 保留筛选后的未归属数量并展示 | 已修复，查询与 Chromium 验证通过 |
| SK-R8 | P2 | refresh.py 清单 | 验证控制总额后未落入运行清单 | 将逐日 control 保存至 date_manifest | 已修复，历史保留及控制量用例通过 |

短链字典覆盖经核实不构成重复归属缺陷：源表有全局 `UNIQUE KEY uk_short_code(short_code)`，大小写不敏感索引可能重复返回同一行，最终 Python 仍按精确字符串匹配。未知任务状态保留原值；新用户充值及充值人数是源表汇总，不声称跨日期去重。

## 编译 / 验证结果

- `python -m unittest tests.test_socialkit_integration tests.test_socialkit_query`：19 项通过（增加前端导航对照测试之前）。包括匿名/令牌/无权限拒绝、原导航 ACL、版本切换、未发布版本拒绝、缺失日期与零值、CSV、过滤和只读快照。
- 新增 `test_browser_fallback_preserves_backend_authorization` 执行实际 JS 迁移段，与后端结果对照，并验证原有项权限不被放宽。
- 技能 `quick_validate.py` 使用 Python UTF-8 模式验证仓库和安装副本通过；CLI 帮助、参数校验与 shell 引号字面量检查通过。
- 最终本地执行按类去重后的 47 个用例全部通过，耗时 8.075 秒；包括 2 个真实 Chromium fixture 页面用例与 2 个无网络部署模拟用例。详见 test-report.md。
- 首页 JSON 预计算检查同已发布版本和默认参数，读取失败回到本地 SQLite；保留 current/previous 两版本的 JSON，未引用文件清理受目录边界约束。
- 10 个本功能 Python 模块通过 Python 3.9 grammar 检查，两 JS 语法检查通过。服务器 Python 3.9 真实执行待部署验证。
- 真实源库回填、线上登录/UI、SQL 性能、定时器启用与回滚结果由 `test-report.md` / `deploy.md` 记录。

最后复核：仓库事实清理按 current 与 previous 的全部 (version,date) 引用并集保护历史；无引用日期以固定表、精确条件、每批 LIMIT 1000 清理。两类清理共享 600 批/120 秒上限，未完成保留运行记录并显示 warning；旧版仅有 previous 引用的日期有独立测试。最终 47 个去重用例通过。
