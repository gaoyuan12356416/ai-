# SA 代码评审

## 结论

2026-11-02 最终审查通过。核心授权、查询、采集和回滚边界已独立复核，发现的问题均已修复。最终功能代码为 86e0b0dca9d01719121251334ced099797157211；56 个独立用例分组通过，真实 14 日快照与部署模块完成 795 项检查。线上数据/性能及验证方法详见 test-report.md 和 deploy.md。

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
| SK-R9 | P1 | collector.py Gate 生命周期 | 14 日流式读在外源归属/写仓等待期间占 Gate，实际回填超过单次 600 秒预算 | 每日 facts/control 同 RR 完整读取退出后再归属与 yield | 已修复，最终含有限瞬时读取重试的 23 项数据用例通过 |
| SK-R10 | P2 | query.py | 真实收入冷查询部分超过 2 秒 | 同请求聚合复用、按需标签、相同过滤未归属行单次读取 | 已修复，20 项 query 用例及 54 次真实性能均通过 |
| SK-R11 | P2 | 技能 query_report.py | Windows helper 将 CSV CRLF 二次翻译产生空行 | 原始 bytes 捕获，仅撤回一次 helper 翻译，buffer 输出 | 已修复，2 项字节回归及真实 CSV 无空记录验证通过 |

短链字典覆盖经核实不构成重复归属缺陷：源表有全局 `UNIQUE KEY uk_short_code(short_code)`，大小写不敏感索引可能重复返回同一行，最终 Python 仍按精确字符串匹配。未知任务状态保留原值；新用户充值及充值人数是源表汇总，不声称跨日期去重。

## 编译 / 验证结果

- 56 个独立用例按实际受影响模块分组执行通过：7 integration、20 query、23 data、2 Chromium、2 deploy、2 skill；unittest 按具体类去重核实数量。详见 test-report.md 的执行批次。
- Python 3.9 grammar、JS 语法、本地真实 Chromium fixture、服务器实际数据测试和公开页面均有验证；未以编译或空数据代替真实验收。
- query 未归属优化复用同一 SQL WHERE/参数/search，按原分组精确汇总整数与 `(site_id,short_code)` 去重；不跨请求共享 context，不改变版本。实测 795 项检查、54 次计时通过，最慢 1.7669 秒。
- 每日读取在同一只读 RR 中完成 facts/control，消费尾部退出状态后释放 Gate；瞬时连接故障最多 3 次，丢弃整次 partial 输出，认证/SQL/控制错误不重试。错误可见信息仅固定安全诊断白名单。
- 清理按 current/previous 的全部 `(version,date)` 并集保护历史，固定表精确条件、LIMIT 1000、共用 600 批/120 秒预算；未完成保留 run 并告警。首页 JSON 仅保留当前/上一版，路径受数据根约束。
- 技能可发现目录/精确显示名/UTF-8/副本一致及凭据扫描通过；实际 CLI JSON/CSV 的版本、金额、逐日控制量一致，CSV 无多余空记录。
- 最终合并回滚包 before-socialkit-86e0b0d 已只读检查：20 个当前哈希匹配，app 精确逆向/AST、两份 nav 精确逆向/Node 通过，pipeline monitor 保留。未执行生产回滚，未更改源业务记录。
