# 测试用例

全部外部 HTTP/文件发布/统一表为 fake 或 temp；禁止真实短链、YouTube、评论和服务器写入。

| 组 | 核心用例 |
|---|---|
| 输出/迁移 | `random_template_video` 权威键 true 全链路；四项默认 false；零输出拒绝；旧错误键只归一输入；旧 cover/naming accept-ignore-default；历史 migration backup/dry-run/apply/idempotency/rollback/并发 |
| 随机模板 | source 枚举；315/无 light；manual 四层；manifest/file SHA；冻结/retry；内部 source 不外返；exact result |
| 操作/UI | 单/多素材；copy video/cover；cover-only 显示“无可用视频产物”且 short/YT disabled；真实 Playwright fake API 验证 modal 顺序 fetch；Clipboard primary/fallback/error；recipe 版本/source/layers 审计展示 |
| 短链 | unique(job,kind)；独立 ID；gy path；base/order/encoding；content/job；仅 fbclid；无 redirect；atomic/idempotent/concurrency/conflict；缺 writer阻断 |
| 宏 | description replace-all；无宏不生成；短链失败零 mutation；冻结 template/rendered；title/comment 不解析；渲染后 bytes |
| OAuth | app/status/refresh/upload/identity；list 每候选实际 refresh+mine；失败隐藏且 mutation=0；upload-only 不列；测试频道隐藏；mine exact；empty/multiple/mismatch/401；pre-mutation transient |
| 上传 | title/description；allowlist；download size/hash/ffprobe；session-before-PUT；308/5xx/404；submitted/processing/visibility/public |
| 防重 | operation；published 二次确认；processing/unknown block；lease renew/generation；两 worker stale writes 全拒绝 |
| 评论/同步 | published 后评论；force-ssl；comment-only retry/unknown；outbox；受控 RPC configured success/missing/auth/redirect/unknown；三表 whitelist/concurrency1/external-ID idempotency/缺表关闭/无 DDL DELETE secrets |
| HK | release/current、unit、8787/18788/legacy18787、asset SHA、无 YT creds、ads unit untouched、health/render/drain/no fallback/rollback |

门禁：focused Python、Python 3.9 parse/compile、JS syntax/browser-safe、相关 broad、`git diff --check`、旧合同 rg、secret scan、scope。
