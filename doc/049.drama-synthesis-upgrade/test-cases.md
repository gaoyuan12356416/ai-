# 测试用例

全部外部 HTTP/文件发布/统一表为 fake 或 temp；禁止真实短链、YouTube、评论和服务器写入。

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
| 评论/同步 | published 后评论；force-ssl；comment-only retry/unknown；outbox；受控 RPC configured success/missing/auth/redirect/unknown；18837 固定且不碰 FB 18836；映射现有 legacy 三表全部必填字段；负数 synthetic queue 保持 video/publish-log join 且不碰现有正数队列；三实体 exact keys/types/required；extra/missing/type 与三 identity mismatch 拒绝；坏 JSON/非 object/contract error fenced finish failed；RPC token/DB credential 成对配置、当前账号 owner、精确 0600、>=32；无 DELETE/任意 SQL/runtime DDL/secrets |
| 统一 MySQL 迁移 | 固定 cluster/host/schema；独立 migrator 与 runtime writer；精确三表 grants、无 wildcard/extra/grant option；全量 legacy column type/NULL/default/charset/collation/extra fingerprint；dry-run 不写；apply 必须 fresh API backup evidence + restore rehearsal PASS；三张表逐一添加 nullable ASCII external-id 列与唯一索引；`ALGORITHM=INPLACE, LOCK=NONE`；二次运行幂等；runtime verify 仅 SELECT/INSERT/UPDATE |
| 文件权限 | app root/owner 必须成对；三层目录 exact owner 0750；Nginx access/default ACL；产物 0640；随机 temp 文件避免崩溃残留命名冲突；现有 X 200、YouTube 404/POST403 |
| HK | release/current、unit、8787/18788/legacy18787、asset SHA、无 YT creds、ads unit untouched、health/render/drain/no fallback/rollback |

门禁：focused Python、Python 3.9 parse/compile、JS syntax/browser-safe、相关 broad、MySQL migration/RPC、`git diff --check`、旧合同 rg、secret scan、scope。

浏览器回归普通仓库命令：`npx --yes --package @playwright/test playwright test scripts/drama_synthesis_browser.spec.js --reporter=line --workers=1`。spec 内置 CLI-relative import resolver，不依赖手工 `NODE_PATH`，不 vendor `node_modules`。
