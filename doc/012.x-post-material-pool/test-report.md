# 012.x-post-material-pool 测试报告

## 测试结论

入池即时 X 校验与素材源 URL 直链增量已部署生产，代码与生产 GO。精确 commit `00b5b088af76dce4a02866beaf0186713daa46fb` 在本地和服务器均完成 143/143 X 回归；现有两条池记录已按 selector 回填为“不可用”，预览 URL 与缺失态符合要求。本轮未调用真实 X。

## 测试范围

- 素材池添加、规范化、FIFO、检查结果、查询和删除。
- manual selector 的无 insight 路径、违规/危险标签、剧映射和查询异常。
- daily runner 的三账号、存储/媒体预检、三条成组、known/unknown 和停止语义。
- SQLite migration、全局 material/account-day/pool 唯一约束和成功态联动。
- Sidecar backend/daily 鉴权、管理员 API、DOM/URL allowlist 和 no-store。
- 素材预览按当前页 ID 批量精确读取 `ads_custom_source.url`、仅返回安全 HTTPS、页面直链和失败关闭。
- 入池复用正式 selector，合规/不合规/不存在/校验服务异常的原子状态写入与 fail closed。
- 既有 X publish、OAuth、账号 owner/admin、短链和 canary 回归。

## 已执行结果

| 命令组 | 数量 | 结果 | 说明 |
| --- | ---: | --- | --- |
| pool + pool selector + daily + ledger + app contract | 69 | 69 通过 | 含入池即时校验、素材 URL 直链和页面契约 |
| X Post service + X accounts + owner backfill | 74 | 74 通过 | 最终关键修订后执行 |
| 合计 | 143 | 143 通过 | 0 失败、0 阻塞 |
| `python -m py_compile`（实际目标） | — | 通过 | app/oauth/client/service 与新增测试 |
| Node `vm.Script` 页面内嵌脚本校验 | 3 段 | 通过 | `x-post-material-pool.html` |

## 生产验收结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 精确 release | 通过 | `00b5b088af76dce4a02866beaf0186713daa46fb`；服务器 release clean，主后台 app/client/x_posts、服务静态页和公网页面哈希与 release 一致 |
| 生产 MySQL | 通过 | 只读 selector：`5503209` 因 1352 秒超过 X 140 秒上限不合格；`11761405635` 在当前源库无记录 |
| SQLite | 通过 | 本次无 schema 迁移；回填前后 pool/queue/log 均为 `2/1/1`，两条仅更新检查字段，`integrity_check=ok` |
| Sidecar/主后台/timer | 通过 | active/active/active，daily service inactive |
| 内部接口 | 通过 | 两条均返回 `validation_failed`；`5503209` 的 `material_preview_url` 为安全 MyQcloud HTTPS，`11761405635` 为空 |
| 公网接口/页面 | 通过 | 匿名管理 API 401 + no-store；公网页面 200，与精确 release hash 一致，页面不再引用旧 preview 跳转端点 |
| 管理员浏览器 | 受限 | Chrome 原标签页登录态已过期，仅显示“登录”；未代替用户登录，接口/页面代码与服务端数据已分别验收 |
| 原有证据保护 | 通过 | 原 canary queue/log 各 1 条，部署未新增 queue/log/Post |
| 配置与秘密 | 通过 | 敏感值未变化，daily env 0400、Sidecar env/SQLite 0600；部署后无 warning 级服务日志 |

## 评审发现

- pool-first 后非池 queue 的跨表占用反例推动了服务层和 SQLite 触发器双重修复；新增 legacy/canary 防御查询与删除回归通过。
- manual selector 已在 SQL 和行级精确校验 `ads_custom_source.product='Dramawave'`；其他产品负例通过。
- `query_pool.summary.available` 已排除 `validation_failed`，专项断言通过。
- runner 已按 scan limit 读取最老 1000 条，再保留最多 50 条合规候选供媒体补位，避免前 50 条不合规直接遮挡。
- 超过 100 条的检查结果已按 API 上限分批；205 条 100/100/5 回归通过。
- 入池即时校验直接复用 manual selector；Sidecar 只接受与本批素材一一对应的检查结果，缺失时 pending/不可用，非法集合整批回滚。
- 素材池列表按当前页精确批量读取源 URL；不合规但源 URL 安全的素材仍可预览，不存在/HTTP/凭据/控制字符/异常端口全部显示无法预览。

## 未执行

- 未调用 X 媒体上传/发帖接口，未创建新的真实 X Post。
- 未做首轮自然 timer 验收。

## 发布建议

生产已按 GitHub-first 部署本次精确 commit，现有两条记录均不可用且池内不足三条，2026-07-24 10:00 CST 的自然任务会按设计整批不发。应删除或修复无效 ID，并录入至少三条可校验的 Dramawave 素材；首轮真实发布仍由自然调度验收，不手工触发 daily service。
