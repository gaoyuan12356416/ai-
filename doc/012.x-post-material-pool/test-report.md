# 012.x-post-material-pool 测试报告

## 测试结论

入池即时 X 校验与素材源 URL 直链增量的本地代码 GO，完整 X 回归为 143/143。生产仍运行上一版素材预览实现，待精确 commit 部署并回填服务器/浏览器证据；本轮测试未调用真实 X。

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

下表为上一版生产基线；本次 143 项增量尚未部署，部署后覆盖更新。

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 精确 release | 通过 | Sidecar/daily 保持 `75f46e77b46f1cda6f05b53b02a96002c75b4bf6`；主后台接口与素材池静态页精确来自 `9711ef77809e53ec4159b0b7f8bd6fe86fdc23d4`，服务器 release clean、部署 hash 一致 |
| 生产 MySQL | 通过 | product 字段存在，Dramawave 样本命中，只读会话 |
| SQLite 副本/正式迁移 | 通过 | 初始迁移账号/run/queue/log/pool=`10/0/1/1/0`；预览增量部署后 pool/queue/log=`2/1/1`，`integrity_check=ok`，8 个跨表保护 trigger 不变 |
| Sidecar/主后台/timer | 通过 | active/active/active，daily service inactive |
| 内部接口 | 通过 | 初始上线时管理员 query、daily available 均 200 且池为 0；当前登录态列表正常返回 2 条 |
| 公网接口/页面 | 通过 | 匿名素材预览 API 401 + no-store；公网素材池页与精确 release hash 一致 |
| 管理员浏览器 | 通过 | 明细显示独立“素材预览 / Post 预览”列；`5503209` 302 到实际 MP4，`11761405635` 因源记录/URL 不可解析返回安全 404 |
| 原有证据保护 | 通过 | 原 canary queue/log 各 1 条，部署未新增 queue/log/Post |
| 配置与秘密 | 通过 | 敏感值未变化，env/DB 权限保持 0400/0600 |

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

待按 GitHub-first 部署本次精确 commit 后，对生产现有池记录做一次只更新校验字段的回填，并确认 timer 仍为 active、daily service inactive、queue/log/Post 计数不变。首轮真实发布仍由自然调度验收，不手工触发 daily service。
