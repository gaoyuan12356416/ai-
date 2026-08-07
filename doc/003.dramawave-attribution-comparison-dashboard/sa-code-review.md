# SA 代码评审

## 结论

D7/D10 代码设计和生产验收通过。数据正确性、只读边界、原子刷新、API 白名单、D10 缓存语义门禁及 `2026-08-01` 日期边界未发现新的设计级 P0；release `ec6fba1b7e155be904c4c54791fd491c76e09a9a` 已使用独立 D10 SQLite 上线，并通过真实对账、已授权页面及自然 timer 验证。2026-08-06 的 canary/bootstrap、systemd/Nginx、缓存预热和自然 timer 证据仍仅作为历史 D30 基线；D10 的生产通过以 2026-08-07 新证据为准。

## 评审范围

- `common.py`：D10 SQLite schema、索引、汇总层、ID/指标定义和缓存语义标记。
- `refresh_cache.py`：只读源连接、同日一致性快照、分层映射、金额守恒、staging/版本提交、60 天裁剪。
- `service.py` / `index.html`：查询白名单、汇总路由、比率重算、质量语义、ETag/gzip、分页/CSV 和前端竞态。
- `deploy/*`：独立 venv、数据盘挂载、systemd hardening、Nginx server-context include、备份和回滚。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | revenue 映射 | 存在 Ad ID 但未命中时若继续 fallback 到 Ad Set，会静默归到错误广告 | 只有更细 ID 本身为空时才 fallback；存在但未命中直接排除并审计 | 已修复并回归 |
| CR-002 | P0 | revenue 聚合 | Android/iOS、data_source 多行直接 Join 会放大收入/花费 | 两张 revenue 先按 Campaign/Ad Set/Ad 合并，再映射 custom 维度 | 已修复并回归 |
| CR-003 | P0 | 多日刷新 | 逐日覆盖会在中途失败时留下混合版本 | 全部日期 staging 成功后以单个 `BEGIN IMMEDIATE` 事务替换并推进版本 | 已修复并回归 |
| CR-004 | P1 | 缓存完整性 | 日期空洞若按 0 展示会误导分析；缺版本的 health 不能返回 200 | 查询区间逐日校验；health 要求事实、版本、连续性同时成立 | 已修复并回归 |
| CR-005 | P1 | 查询性能 | 30 万行合成事实原实现冷查询约 6.8 秒；生产单日最细事实实测 75,283～131,011 行，单纯 LRU 不可放行 | 事务内维护筛选级/Campaign级日汇总，按查询维度选最小层；新版本提交后强制预热常用全范围路径 | 已修复并通过 D10 生产实测；重复暖查询 `0.0027s`。历史 D30 常用路径预热后均 `<3 ms`，未预热低基数组合约 `248 ms` |
| CR-006 | P1 | 部署 | unit 未绑定数据盘、Nginx 片段可能放错 context、PyMySQL 未固定环境 | `RequiresMountsFor`、明确 `/etc/nginx/default.d`、独立 pinned venv | 已修复并通过目标机验证 |
| CR-007 | P1 | 映射质量 | 候选内 `unmapped=0` 不能代表全源没有被排除的 revenue | 从每日最新成功刷新日志返回日期级全源排除量，并标明不可归属业务筛选 | 已修复并回归 |
| CR-008 | P2 | health 语义 | 当天尚未产数或缓存超过 45 分钟是否应直接 503 | 日期连续性决定结构健康；`stale` 和 `current_date_present` 单独告警，保留上一成功版本可读 | 设计接受 |
| CR-009 | P0 | 缓存复用 | D10 代码可能误开历史 D30 SQLite，并把旧数据当作新口径 | 要求结构签名及 `comparison_window=D10`、`new_attribution_source=kunlunads_dev.ads_app_revenues_10d` 完全匹配；不匹配时 Web/刷新失败关闭 | 已实现；旧 D30 immutable 拒绝门禁与独立 D10 生产库验证通过 |
| CR-010 | P0 | 日期边界 | 原代码最小日期为 7/29，而批准后的合同起点为 8/1 | `MIN_DATE`、前端 fallback、边界测试和 bootstrap 统一改为 8/1；不允许参数绕过 | 已修复并通过生产验证 |

## 编译 / 验证结果

- 2026-08-06 历史 D30 基线：Windows/目标机 Python `51/51`、编译、Python 3.9 兼容、浏览器 fixture、只读源快照、单日 canary、`920,751` facts bootstrap、systemd/Nginx 和自然 timer 均按当时记录通过。
- 2026-08-07 D10 已核验事实：`ads_app_revenues_10d` schema/index 与 D30 相同，当前最早日期为 `2026-08-01`。
- 2026-08-07 D10 自动化验证：本地及目标机自动化 `64/64` 通过，覆盖 D10 字段/源表合同、60 天裁剪及旧缓存拒绝；旧 D30 SQLite 的 `mode=ro&immutable=1` 拒绝门禁通过，D10 主文件配合未 checkpoint 的已提交 WAL 篡改也由 WAL-aware 二次合同校验拒绝，refresh 不进入 writable/MySQL，Web 不启动 HTTP Server。
- 2026-08-07 D10 生产发布：release `ec6fba1b7e155be904c4c54791fd491c76e09a9a` 以 `2026-08-01` 为起点，活动库为独立 `dashboard-d10-6c323f5ff35e-20260807-175911.sqlite3`；历史 D30 库和备份保持不变并继续作为回滚点。
- 2026-08-07 D10 数据验收：一个真实唯一 Ad 的 custom、D7、D10 结果精确对账通过；生产重复暖查询为 `0.0027s`。
- 2026-08-07 D10 页面验收：已授权 Chrome 页面加载、Meta 筛选和 D7 累计切换通过，数据版本为 `20260807T103755Z-96c22ea1`，未发现功能错误；仅观察到浏览器扩展启动时 3 条 `message-channel` 噪声，与页面功能无关。
- 2026-08-07 D10 自然调度：timer 为 `*:04,34`；`18:34:10`～`18:38:33` 自然运行成功，刷新 `2026-08-07`、`2026-08-06`、`2026-08-01`，版本由 `20260807T101302Z-28d37de4` 推进到 `20260807T103755Z-96c22ea1`，预热成功。
- 回滚边界：历史 D30 数据库与备份已保留，发布流程具备回滚入口；本次未执行完整实际回滚演练，因此不将其记录为“完整回滚演练通过”。

## 变更记录

- 2026-08-07：评审对象由 D7/D30 改为 D7/D10；新增旧 D30 SQLite 拒绝门禁。用户批准 8/1 起点后，代码、前端和测试边界已统一；目标机 `64/64`、独立 D10 SQLite、真实对账、已授权页面和自然 timer 均通过，D10 生产验收收口。历史 D30 验证结果保持原值并仅作为容量与回滚基线；完整实际回滚演练未执行。
