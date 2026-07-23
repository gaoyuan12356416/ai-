# 012.x-post-material-pool 测试报告

## 测试结论

代码测试 GO，生产部署待门禁。跨表双键占用、Dramawave 产品门禁、summary.available 口径、1000/50 两级扫描窗口和检查回写分批修订后，最终工作树离线专项与全部 X 回归 139/139 通过。未执行生产 MySQL/SQLite、服务或真实 X 验收。

## 测试范围

- 素材池添加、规范化、FIFO、检查结果、查询和删除。
- manual selector 的无 insight 路径、违规/危险标签、剧映射和查询异常。
- daily runner 的三账号、存储/媒体预检、三条成组、known/unknown 和停止语义。
- SQLite migration、全局 material/account-day/pool 唯一约束和成功态联动。
- Sidecar backend/daily 鉴权、管理员 API、DOM/URL allowlist 和 no-store。
- 既有 X publish、OAuth、账号 owner/admin、短链和 canary 回归。

## 已执行结果

| 命令组 | 数量 | 结果 | 说明 |
| --- | ---: | --- | --- |
| pool + pool selector + daily + ledger + app contract | 65 | 65 通过 | 最终关键修订后执行 |
| X Post service + X accounts + owner backfill | 74 | 74 通过 | 最终关键修订后执行 |
| 合计 | 139 | 139 通过 | 0 失败、0 阻塞 |
| `python -m py_compile`（实际目标） | — | 通过 | oauth/client/service/selector/runner |
| `node --check static/quick-nav.js` | — | 通过 | 无输出、exit 0 |

## 评审发现

- pool-first 后非池 queue 的跨表占用反例推动了服务层和 SQLite 触发器双重修复；新增 legacy/canary 防御查询与删除回归通过。
- manual selector 已在 SQL 和行级精确校验 `ads_custom_source.product='Dramawave'`；其他产品负例通过。
- `query_pool.summary.available` 已排除 `validation_failed`，专项断言通过。
- runner 已按 scan limit 读取最老 1000 条，再保留最多 50 条合规候选供媒体补位，避免前 50 条不合规直接遮挡。
- 超过 100 条的检查结果已按 API 上限分批；205 条 100/100/5 回归通过。

## 未执行

- 未连接生产 MySQL 验证真实 product 字段/数据。
- 未迁移生产 SQLite 或生产副本。
- 未启动/重启生产服务或 timer。
- 未使用真实 OAuth Token、未上传媒体、未创建真实 X Post。
- 未做首轮自然 timer 验收。

## 发布建议

代码可进入 GitHub-first 发布准备，但本轮不部署。生产只读 product/schema 抽样、SQLite 副本迁移、live composite 基线和精确 commit/release 全部通过后，方可启用 timer；首轮真实发布由自然调度验收。
