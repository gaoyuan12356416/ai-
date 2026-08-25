# SA 代码评审

## 结论

本地代码评审通过；真实 MySQL 阴影运行、Nginx 语法和生产浏览器回归完成前，不建议正式发布。

## 评审范围

- `ops/ai-game-performance/` 生成器、前端、Nginx 配置和测试；
- `deploy/ai-game-performance-*` unit/timer/env；
- 双事实口径、SQLite 事务、发布提交点、密钥保护、数据盘与回滚边界。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | 高 | `mysql_command_env` | `/opt` release 无法隐式导入 `/root/codex_test` 模块 | 显式可配置只读模块目录并加回归测试 | 已修复，BUG-002 |
| CR-002 | 中 | 前端最低花费 | 原实现先过滤原始行，会丢掉汇总后超过阈值的组合 | 先按选定维度汇总，再用组键回筛原始行 | 已修复 |
| CR-003 | 中 | 增量窗口 | 仅今天/昨天可能漏掉 D1 次日回补 | 默认刷新今天及前两天 | 已修复 |
| CR-004 | 中 | 发布提交点 | `latest.json` 成功后审计写入失败会误报整次发布失败 | 清单为明确提交点；审计改为 best-effort 并返回状态 | 已修复 |
| CR-005 | 低 | 默认游戏维度 | 仅按游戏名分组可能合并同名不同 ID | 默认同时显示/分组游戏名和游戏 ID | 已修复 |
| CR-006 | 低 | 版本清理 | 清理函数可能删除 `data/` 下非版本目录 | 仅删除匹配严格版本格式且超过 24 小时的目录 | 已修复 |
| CR-007 | 中 | Nginx/auth | 新 location 复用 TT 鉴权子请求，需验证命名 location 与登录 next | 服务器 `nginx -t`、匿名/登录态回归 | 待生产验证 |
| CR-008 | 中 | 性能/容量 | 单日转化文件最高约 5 万源行，需验证全量刷新内存、磁盘和浏览器 | 数据盘阴影全量、文件大小/耗时/390px 浏览器回归 | 待生产验证 |
| CR-009 | 高 | 手工转化 SQL 日期格式 | `.format()` SQL 模板错误保留 `%%`，MySQL 返回字面量 `%Y-%m-%d` | 改为 MySQL 单 `%` 格式符，并增加 SQL 级回归 | 已修复，BUG-003 |
| CR-010 | 高 | 平均游戏时长 | 现网“总播放时长”被再次乘安装数，派生均值虚高 | 总时长直接求和后除安装；历史均值字段读取时转总量 | 已修复，BUG-004 |
| CR-011 | 低 | CSV 导出 | Blob URL 点击后立即释放，浏览器自动化无法稳定观察下载 | 锚点挂入 DOM，下载后延迟释放并增加 BOM/CRLF/生命周期契约 | 已修复，BUG-005 |

## 编译 / 验证结果

- `python -m py_compile ...`：PASS；
- `python -m unittest discover ...`：18/18 PASS；
- `python validate_frontend_contract.py`：PASS（含 `node --check`）；
- `git diff --check`：PASS。

剩余验证：真实只读 SQL、SQLite 全量快照、Nginx/systemd、生产 HTTP 与浏览器。
