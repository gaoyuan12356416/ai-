# SA 代码评审

## 结论

通过。数据正确性、只读边界、原子刷新和 API 白名单未发现未解决 P0；真实 canary/bootstrap、目标 Linux systemd/Nginx、缓存预热和第一轮自然 timer 均已通过。生产已授权飞书会话的视觉检查仍需首次登录用户补验，但不影响服务端发布结论。

## 评审范围

- `common.py`：SQLite schema、索引、汇总层和 ID/指标定义。
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
| CR-005 | P1 | 查询性能 | 30 万行合成事实原实现冷查询约 6.8 秒；生产单日最细事实实测 75,283～131,011 行，单纯 LRU 不可放行 | 事务内维护筛选级/Campaign级日汇总，按查询维度选最小层；新版本提交后强制预热常用全范围路径 | 已修复并通过生产实测；常用路径预热后均 `<3 ms`，未预热低基数组合约 `248 ms` |
| CR-006 | P1 | 部署 | unit 未绑定数据盘、Nginx 片段可能放错 context、PyMySQL 未固定环境 | `RequiresMountsFor`、明确 `/etc/nginx/default.d`、独立 pinned venv | 已修复并通过目标机验证 |
| CR-007 | P1 | 映射质量 | 候选内 `unmapped=0` 不能代表全源没有被排除的 revenue | 从每日最新成功刷新日志返回日期级全源排除量，并标明不可归属业务筛选 | 已修复并回归 |
| CR-008 | P2 | health 语义 | 当天尚未产数或缓存超过 45 分钟是否应直接 503 | 日期连续性决定结构健康；`stale` 和 `current_date_present` 单独告警，保留上一成功版本可读 | 设计接受 |

## 编译 / 验证结果

- Windows/目标机 Python：`51/51` 自动化测试通过，包含汇总守恒、路由回退、排行 singleflight、缓存预热、磁盘 staging、共享锁和前端并发契约。
- `python -m compileall -q ops/dramawave-attribution-comparison`：通过。
- `git diff --check`：通过。
- Python 3.9 AST/编译兼容复核：通过。
- 本地真实浏览器 fixture：桌面/移动布局、筛选、D0/D7、分页、无明细下载、控制台 0 错误通过。
- 生产只读源：63350 且 `@@read_only=1`；`REPEATABLE READ + READ ONLY, WITH CONSISTENT SNAPSHOT` 在 autocommit on/off 均验证通过。
- 生产证据：单日 canary 峰值约 772 MiB；全量 `920,751` facts bootstrap 成功；`systemd-analyze verify`、`nginx -t`、未登录飞书跳转和自然 timer 版本推进通过。已授权飞书会话视觉检查待首次登录用户补验。
