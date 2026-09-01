# 012.x-post-material-pool 测试报告

## 2026-09-01 Post 发布明细任务来源

- 发布明细新增 `task_source` 列与筛选：`drama_pool=短剧池`、`material_pool=素材池`、`auto_publish=自动发布`。素材池口径包含素材池定时批次和人工素材池批次；自动发布仅由关联 manual run 的 `trigger_source=auto_template` 判定。
- X release 使用 GitHub commit `401069b2e35e56192c33efac623bf24ddee57a56`，部署到 `/mnt/data-disk/x-post-automation/releases/401069b2e35e56192c33efac623bf24ddee57a56`。主 API 保留 2026-09-01 剧集合成现行 `app.py` 基线，仅叠加查询参数透传，commit 为 `c42ce20dd7d75e7cab951545614bcf62eab32845`。
- 本地与服务器精确 X release 均通过 299 项回归；现行主 API release 另通过参数解析和 Cookie/no-store 路由 2 项回归；内嵌 JavaScript、Python 编译和 `git diff --check` 通过。Playwright mock 验证三种来源同时渲染，选择“自动发布”后请求包含 `task_source=auto_publish` 且只显示对应行。
- 生产只读验证覆盖全部 1,142 条 queue：短剧池 520、素材池 585、自动发布 37，合计与未筛选总数一致。部署前后 queue/log/published 为 `1142/1142/1108`，active queue/manual/schedule 均为 0；历史 `unknown_outcome=1` 仍为 1 条（queue/log 726），未重试或改写。
- SQLite `quick_check=ok`；Token 文件哈希、`0600` 权限和 owner 未变化。Sidecar、主 API、六个原 active timer 均恢复 active；自然轮询返回 manual `no_pending`、claim 0、schedule `no_due`，没有因部署创建、重放或发布真实 X Post。
- 回滚包：`/mnt/data-disk/x-post-automation/backups/20260901T181954+0800-x-post-log-source-401069b`。回滚时先停止相同六个 timer 并等待 oneshot 排空，切回 `/mnt/data-disk/x-post-automation/releases/e300542887fb89314bef145b752c3ad8aa6c5c9c`，从包内恢复主 API/客户端/service/两份静态页，重启 `x-post-automation.service` 与 `drama-material-api.service`，再按 `timer-state.txt` 恢复 timer。默认保留当前 SQLite 与 Token，不用备份覆盖后来事实。

## 测试结论

生产当前精确 commit 为 `622a8caff321dc297871d7cea354ad8d5fed4e52`。仅 X Post selector 已取消 `drama_labels` 色情/暴力内容词拦截，其他渠道、违规历史、素材源/资源危险标签和既有权限边界均未修改。本次本地和服务器均完成 143/143 X 回归；旧 `drama_label_unsafe` 三条记录重校验为可供发布，未手工触发 daily、未调用真实 X。

## 2026-07-24 X Post 短剧标签增量

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 规则边界 | 通过 | 仅移除 X manual/legacy selector 的短剧 labels 危险词拒绝；source tag、resource tag、四类违规证据继续拒绝 |
| 正向用例 | 通过 | `Sexual Content,Graphic Violence` 作为 drama labels 时可进入 X 候选，首个 label 仍用于归因 |
| 本地回归 | 通过 | 143/143；Python 编译、Node 语法、`git diff --check` 均通过 |
| 服务器回归 | 通过 | GitHub 精确 commit `622a8ca` 同组 143/143 通过后才切换 release |
| 生产重校验 | 通过 | 池 ID `17/18/19`、素材 `5580542/5399394/5307937` 均清空错误；`drama_label_unsafe` 剩余 0 |
| 无发布副作用 | 通过 | 重校验前后 run/queue/log 为 `1/2/2`，pool 为 32；未创建 Post |
| 运行状态 | 通过 | 主后台/Sidecar/timer active；health/public page 200；下次 timer 为 2026-07-25 10:00 CST |
| 配置保护 | 通过 | Token hash/mode 未变化，SQLite `integrity_check=ok`，部署后 warning 级日志为 0 |

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
| pool + pool selector + daily + ledger + app contract | 71 | 71 通过 | 含导航配置授权、入池即时校验、素材 URL 直链和页面契约 |
| X Post service + X accounts + owner backfill | 74 | 74 通过 | 最终关键修订后执行 |
| 合计 | 145 | 145 通过 | 0 失败、0 阻塞 |
| `python -m py_compile`（实际目标） | — | 通过 | app/oauth/client/service 与新增测试 |
| Node `vm.Script` 页面内嵌脚本校验 | 1 段 | 通过 | `x-post-material-pool.html` |

## 生产验收结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 精确 release | 通过 | `3d5ba0b0cc708a3d49dda43b8d59cf0b179ad1c8`；主后台 app/client、Sidecar oauth/client、服务静态页和公网页面均与 release 一致 |
| 历史素材校验 | 通过 | 只读 selector 曾确认：`5503209` 因 1352 秒超过 X 140 秒上限不合格；`11761405635` 在当时源库无记录 |
| SQLite | 通过 | 权限部署前后 pool/queue/log 均为 `1/1/1`，`integrity_check=ok`；没有因权限验收写入素材池、queue 或发布日志 |
| Sidecar/主后台/timer | 通过 | active/active/active；2026-07-24 10:00 自然任务因池内不足三条记录 `failed_preflight/x_post_daily_pool_shortage`，未创建 queue/log/Post；下次触发 2026-07-25 10:00 CST |
| 内部接口 | 通过 | 苏斯琪查询当前 `POOL_TOTAL=1`；列表仍按安全 HTTPS 规则附加 `material_preview_url`，不返回内部凭据 |
| 公网接口/页面 | 通过 | 匿名管理 API 401 + no-store；公网页面 200，与精确 release hash 一致，页面不再引用旧 preview 跳转端点 |
| 普通用户实会话 | 通过 | 苏斯琪：topbar 200、`is_admin=false`、`x_accounts=true`、素材池 200；同会话 X 账号配置 200、管理员发布日志 403 |
| 原有证据保护 | 通过 | 原 canary queue/log 各 1 条，部署未新增 queue/log/Post |
| 配置与秘密 | 通过 | 部署时 env/Token hash 未变，最终备份 manifest 通过；次日 10:00 自然任务账号预检窗口有 3 个 Token 文件正常更新，当前 10 个 Token 文件仍全部 0600；部署后主后台/Sidecar 无 warning 级日志 |

## 评审发现

- pool-first 后非池 queue 的跨表占用反例推动了服务层和 SQLite 触发器双重修复；新增 legacy/canary 防御查询与删除回归通过。
- manual selector 已在 SQL 和行级精确校验 `ads_custom_source.product='Dramawave'`；其他产品负例通过。
- `query_pool.summary.available` 已排除 `validation_failed`，专项断言通过。
- runner 已按 scan limit 读取最老 1000 条，再保留最多 50 条合规候选供媒体补位，避免前 50 条不合规直接遮挡。
- 超过 100 条的检查结果已按 API 上限分批；205 条 100/100/5 回归通过。
- 入池即时校验直接复用 manual selector；Sidecar 只接受与本批素材一一对应的检查结果，缺失时 pending/不可用，非法集合整批回滚。
- 素材池列表按当前页精确批量读取源 URL；不合规但源 URL 安全的素材仍可预览，不存在/HTTP/凭据/控制字符/异常端口全部显示无法预览。
- 页面直接 no-store 读取 `/navigation.json`，后端独立读取相同生产配置；主后台放行后只向 loopback Sidecar 的素材池 query/add/delete 附加精确导航授权标记。错误标记仍 403，账号全量列表、发布日志、运行记录和 daily 路由未放权。

## 未执行

- 未调用 X 媒体上传/发帖接口，未创建新的真实 X Post。
- 未出现可发布的三素材批次，因此仍未执行真实媒体上传或 X Post 成功态验收。

## 发布建议

生产已按 GitHub-first 部署本次精确 commit。2026-07-24 10:00 CST 的自然任务已按设计因池内不足三条整批不发；应补充至少三条可校验的 Dramawave 素材，首轮真实发布继续由自然调度验收，不手工触发 daily service。
