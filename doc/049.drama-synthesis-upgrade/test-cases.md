# 测试用例

## 2026-08-27 现行范围与结果边界

用户已授权通过 SSH 完成 HK 环境、部署、三表备份/隔离恢复演练，并在门禁通过后只用 Shahrul Ikmal 执行一次内部 unlisted 视频与一条评论；禁止腾讯云管理后台和 public 测试。正式 HTTP/UI 仍固定 public，内部 CLI 不打开正式 live/sync。CPU 未切流前继续保持 18787，HK 通过 18788 隔离验证，不触碰 X/ads_video_producer。

下表是自动化 fake/temp suite 的测试范围，不声明其执行了真实外部发布。最新候选 c719bebf72be900ec3853858dc53b36b83beffd2 独立合并 166/166 PASS；另 25 文件语法/Python3.9 AST 与 5 项内存媒体对抗 PASS，后两类不叠加 unittest 数。实际 SSH 三表 snapshot/恢复已 PASS，HK auto 媒体基本检查通过但重复 POST 幂等 FAIL；生产 DDL/RPC/YouTube 仍被合法账号权限缺口阻塞，见 [测试报告](test-report.md)。

| 组 | 核心用例 |
|---|---|
| 输出/迁移 | `random_template_video` 权威键 true 全链路；四项默认 false；零输出拒绝；旧错误键只归一输入；旧 cover/naming accept-ignore-default；历史 migration backup/dry-run/apply/idempotency/rollback/并发 |
| 随机模板 | source 枚举；315/无 light；manual 四层；manifest/file SHA；冻结/retry；内部 source 不外返；exact result |
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

门禁：focused Python、Python 3.9 parse/compile、JS syntax/browser-safe、相关 broad、MySQL migration/RPC、`git diff --check`、旧合同 rg、secret scan、scope。

浏览器回归普通仓库命令：`npx --yes --package @playwright/test playwright test scripts/drama_synthesis_browser.spec.js --reporter=line --workers=1`。spec 内置 CLI-relative import resolver，不依赖手工 `NODE_PATH`，不 vendor `node_modules`。

历史说明：2026-08-26 的 fake-only/未授权描述只约束当时的离线轮次，不能覆盖以上最新授权。此前浏览器与旧候选计数保留在 test-report 历史段，不计入本轮 166，也不假称本轮重新跑过浏览器。
