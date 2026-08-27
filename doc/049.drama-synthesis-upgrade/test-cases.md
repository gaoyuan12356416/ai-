# 测试用例

## 最新覆盖范围：现有账号 v3（2026-08-27 16:35）

按用户新决定与 [现行合同](ads-ai-new-tables-20260827.md)，不再创建专用数据库账号。CPU 使用现有 ads_aius 与已有频道授权，应用 SQL 仅限 ads_ai 新三表；原 MySQL 表只读。健康合同为 drama-youtube-writer-preflight-v3，shared-existing-account / application-table-allowlist / db_least_privilege=false；仅核验必要能力，不宣称全量 grant 审计。每次写前验证 TRIGGER 可见性和无 trigger/FK，旧健康合同拒绝。既有 DDL/v2 payload 与 UI 合同不变；下文专用账号/旧 v2 health 是历史。本轮专项 108/108，独立唯一完整回归及实机发布验收另记，不叠加历史批次。

## ads_ai 新表增量

固定 SQL 仅三条 CREATE；同名视图/错误所有权/列索引/触发器/外键均拒绝；全缺、部分兼容、全兼容与并发创建失败均不改原表；高熵独立 writer、最小 grants、v2 health、旧 schema/v1 拒绝；完整 payload Unicode/长字段精确回读、乱序/幂等/冲突；所有旧迁移入口零网络拒绝。真实演练固定新隔离目录/端口和候选 SHA，生产 dry-run、apply、启用前 admin 无 trigger 及 runtime 健康分开取证。canary 继续要求恰好一个 unlisted 视频、一条评论、新三表各一条且原表不写。见 [新表合同](ads-ai-new-tables-20260827.md)。

## 2026-08-27 现行范围与结果边界

最新 CPU 候选 `40042f9692fbec58caa5abbf41af35e9aefb54bc` 将所有业务查询保留 CPU，其中模板目录只读固定 SHA 的本机 manifest；HK 只制作和上传 COS。新增 16 项已进入独立一次七套 **204/204 PASS（13.639 秒）**；另外 15 项纯内存对抗单列 PASS。CPU Python 3.9.6 真实 manifest/原函数隔离验证通过；生产应用未切换。下文 HK 188 与 c719 166 是历史批次，不与 204 叠加。

用户已授权通过 SSH 完成 HK 环境、部署、三表备份/隔离恢复演练，并在门禁通过后只用 Shahrul Ikmal 执行一次内部 unlisted 视频与一条评论；禁止腾讯云管理后台和 public 测试。正式 HTTP/UI 仍固定 public，内部 CLI 不打开正式 live/sync。CPU 未切流前继续保持 18787，HK 通过 18788 隔离验证，不触碰 X/ads_video_producer。

HK 当前 GitHub-first 部署版本为 `e1f5a1d04cfb510df9c2444ac592adec2827508b`；299309 行旧三表恢复演练仍精确绑定 `c719bebf72be900ec3853858dc53b36b83beffd2`，不能改绑最新 CPU 候选。HK 双模式及重启复用已通过，不再沿用 c719 阶段重复 POST 失败的当前结论。

| 证据批次 | 实际执行结果与边界 |
| --- | --- |
| HK 增量六套代码 QA | 共 188 项，首次 187 PASS + 1 项 migration.md 文本合同 FAIL，13.567 秒；补回文档后仅该失败项复测 1/1 PASS，0.050 秒。未改测试/权限/代码，不是一次整套全绿 |
| 静态与冻结检查 | 27 文件语法/Python 3.9 AST、diff-check PASS；5 个代码冻结文件前后 SHA 一致；真实 app/worker/v3 helper import tripwire 无应用级副作用 |
| HK v3 真机 | auto + manual 共 4 输出，两个随机均 720×1280/High/5 秒/150 帧；完整下载解码与封面 callback PASS；fresh 79.44 秒 |
| HK 重启后 replay | 实际重启 worker+tunnel，fixture HTTP 服务未运行时两 job 重复 POST 均 200；manifest SHA/mtime 不变、workdir 无重建，单元 1.710 秒、Result=success |
| 独立证据复核 | 只读核对上述报告与 8 PNG PASS，无新阻断；不算再次真机执行 |

v3 报告为 `/data/drama-synthesis-gpu/work/acceptance/http-media-20260827-v3.json`，SHA-256 `40746316694eeb4d34fb4511713acb13b8de14cff382f6116fdd99f1351f2175`。下表列累计测试范围，不声明本轮重跑全部历史浏览器/fake 用例，也不代表真实 YouTube 发布。旧 166、focused 22 及五项内存 mock 对抗不叠加到 188；详情见 [测试报告](test-report.md) 与 [部署状态页](deployment-status-20260827.md)。

当前剩余门禁：生产 ads_aius 仅 SELECT/SHOW VIEW，无合法 admin/migrator/writer；CPU 主应用未部署/重启、继续 18787，0 真实 YouTube 上传/评论。HK 当前 `drama-synthesis-canary/20260827` 前缀仍须独立备份配置、切 production prefix 后验收，尚不能称正式发布。指定单次 unlisted 测试已有授权，待合法权限/凭据及健康/身份前置条件。

| 组 | 核心用例 |
|---|---|
| 输出/迁移 | `random_template_video` 权威键 true 全链路；四项默认 false；零输出拒绝；旧错误键只归一输入；旧 cover/naming accept-ignore-default；历史 migration backup/dry-run/apply/idempotency/rollback/并发 |
| 随机模板 | source 枚举；315/无 light；manual 四层；manifest/file SHA；冻结/retry；内部 source 不外返；exact result |
| CPU 查询边界 | 原始 manifest metadata-only，无 HTTP/DB/媒体包访问；315/无 light；与 GPU 目录及 auto/manual recipe identity 一致；缺配置/错 SHA 无 fallback；regular/非 symlink/绝对路径/有界长度；文件打开竞态/增长截断/同长改写；重复 JSON/错误版本/类别/字段/名字/大小/UTF8/NaN 脱敏失败；HK 本地诊断保留；CPU 实际文件与原函数隔离验证 |
| 操作/UI | 单/多素材；copy video/cover；cover-only 显示“无可用视频产物”且 short/YT disabled；真实 Playwright fake API 验证 modal 顺序 fetch；Clipboard primary/fallback/error；recipe 审计仅 DOM textContent；hostile img/onerror/script/quotes 0 执行/0 注入且文本可见 |
| 短链 | unique(job,kind)；独立 ID；gy path；base/order/encoding；content/job；仅 fbclid；无 redirect；atomic/idempotent/concurrency/conflict；缺 writer阻断 |
| 宏 | description replace-all；无宏不生成；短链失败零 mutation；冻结 template/rendered；title/comment 不解析；渲染后 bytes |
| OAuth | app/status/refresh/upload/identity；list 每候选实际 refresh+mine；失败隐藏且 mutation=0；upload-only 不列；测试频道隐藏；mine exact；empty/multiple/mismatch/401；pre-mutation transient |
| 上传 | title/description；allowlist；download size/hash/ffprobe；session-before-PUT；308/5xx/404；submitted/processing/visibility/public |
| 防重 | operation；published 二次确认；processing/unknown block；lease renew/generation；两 worker stale writes 全拒绝 |
| 评论/同步 | published 后评论；force-ssl；public/canary 都传冻结 channelId，仅接受 snippet.topLevelComment.id；2xx 缺失/身份不符为 unknown；comment-only retry/unknown；outbox；受控 RPC configured success/missing/auth/redirect/unknown；18837 不碰 FB18836；legacy 三表映射、负数 synthetic queue、三实体 exact keys/identity；坏 JSON/fencing；凭据 exact owner/0600；无 DELETE/任意 SQL/runtime DDL/secrets |
| 内部 canary | app1479/channel263/account255/UCHJ1jFaYuW8g5EM7hM5pPpg；单 operation 与明确 CLI 授权、精确 task claim、真实 job/source；browser/普通 worker/outbox 隔离；session intent 先落盘；processing/succeeded/unlisted；无身份 unknown 与未知评论不盲重试；两操作员复跑不重传/重评 |
| canary P1 门禁 | 任何 refresh/claim/upload 前做鉴权 RPC health/schema/index/exact writer grants；配置存在不等于可用；每个同步前 fresh unlisted readback，完成任务的 pending 重试同样适用；隐私漂移/状态未知 0 新 outbox claim；hold 恢复不重发评论 |
| 统一 MySQL 迁移 | 固定生产 cluster/host/schema/migrator/writer 权限闭包；三表 READ ONLY 一致性快照、rows/schema/index/inventory SHA；CPU loopback23357、固定 digest MySQL5.7、独立空 schema/container/datadir；候选代码/manifest/report/evidence 精确绑定与时效；生产 apply 重验真实 table_snapshot_rehearsal 证据；nullable external-id/unique index、INPLACE/LOCK=NONE、二次 apply/dry-run 幂等、历史 NULL 和 legacy 数据不变 |
| 文件权限 | app root/owner 必须成对；三层目录 exact owner 0750；Nginx access/default ACL；产物 0640；随机 temp 文件避免崩溃残留命名冲突；现有 X 200、YouTube 404/POST403 |
| HK | release/current、unit、8787/18788/legacy18787、完整依赖/本地模型/asset SHA、无 YT creds、ads unit untouched、health/render/cover bypass、并发1；静音/反相/非有限输入；滤镜线程预算、保留源时间轴、容器与视频流固定0.15秒时长容差；重复POST实测门禁；drain/no fallback/rollback |
| HK 缓存与 profile | metadata 绑定 URL/实际 size；HEAD 200、无 redirect、精确 length；坏缓存/暂时不通禁止重制覆盖，legacy 1 MiB 门槛保留；随机 profile 必填且精确，缺失/错值的离线独立对抗在 HEAD 前阻断，有效 profile 真机命中；fresh 与 restart replay 分开，fixture HTTP 服务未运行也不得重建 workdir |

门禁：focused Python、Python 3.9 parse/compile、JS syntax/browser-safe、相关 broad、MySQL migration/RPC、`git diff --check`、旧合同 rg、secret scan、scope。

浏览器回归普通仓库命令：`npx --yes --package @playwright/test playwright test scripts/drama_synthesis_browser.spec.js --reporter=line --workers=1`。spec 内置 CLI-relative import resolver，不依赖手工 `NODE_PATH`，不 vendor `node_modules`。

历史说明：2026-08-26 的 fake-only/未授权描述只约束当时的离线轮次，不能覆盖最新授权。此前浏览器、c719 的 166/166 与 25 文件语法、focused 22 及五项内存 mock 媒体对抗保留为各自历史批次，不并入当前 188 项，不假称本轮重新跑过浏览器或整套回归。
