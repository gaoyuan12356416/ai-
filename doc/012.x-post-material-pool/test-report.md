# 012.x-post-material-pool 测试报告

## 测试结论

代码与生产部署 GO，首轮自然 timer 发布验收待 2026-07-24 10:00 CST。跨表双键占用、Dramawave 产品门禁、summary.available 口径、1000/50 两级扫描窗口和检查回写分批修订后，最终工作树与服务器精确 release 的全部 X 回归均为 139/139。生产只读 MySQL、SQLite 副本/正式迁移、服务/API、权限和登录态页面均已验收；本轮未调用真实 X。

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

## 生产验收结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 精确 release | 通过 | `75f46e77b46f1cda6f05b53b02a96002c75b4bf6`，服务器工作树 clean |
| 生产 MySQL | 通过 | product 字段存在，Dramawave 样本命中，只读会话 |
| SQLite 副本/正式迁移 | 通过 | `integrity_check=ok`；账号/run/queue/log/pool=`10/0/1/1/0`；8 个跨表保护 trigger |
| Sidecar/主后台/timer | 通过 | active/active/active，daily service inactive |
| 内部接口 | 通过 | 管理员 query 200、daily available 200，均返回 0 条 |
| 公网接口/页面 | 通过 | 匿名管理 API 401 + no-store；素材池页、日志页、OAuth health 200 |
| 管理员浏览器 | 通过 | 素材池 0 条、导航正常、console warning/error 0 |
| 原有证据保护 | 通过 | 原 canary queue/log 各 1 条，部署未新增 queue/log/Post |
| 配置与秘密 | 通过 | 敏感值未变化，env/DB 权限保持 0400/0600 |

## 评审发现

- pool-first 后非池 queue 的跨表占用反例推动了服务层和 SQLite 触发器双重修复；新增 legacy/canary 防御查询与删除回归通过。
- manual selector 已在 SQL 和行级精确校验 `ads_custom_source.product='Dramawave'`；其他产品负例通过。
- `query_pool.summary.available` 已排除 `validation_failed`，专项断言通过。
- runner 已按 scan limit 读取最老 1000 条，再保留最多 50 条合规候选供媒体补位，避免前 50 条不合规直接遮挡。
- 超过 100 条的检查结果已按 API 上限分批；205 条 100/100/5 回归通过。

## 未执行

- 未调用 X 媒体上传/发帖接口，未创建新的真实 X Post。
- 未做首轮自然 timer 验收。

## 发布建议

生产已按 GitHub-first 精确 commit 发布，timer 已恢复。素材池初始为空；管理员应在下一次自然触发前录入至少三条可校验的 Dramawave 素材，否则任务会按设计整批不发。首轮真实发布仍由自然调度验收，不手工触发 daily service。
