# 数据与所有权合同

## 最新覆盖范围：现有账号 v3（2026-08-27 16:35）

按用户新决定与 [现行合同](ads-ai-new-tables-20260827.md)，不再创建专用数据库账号。CPU 使用现有 ads_aius 与已有频道授权，应用 SQL 仅限 ads_ai 新三表；原 MySQL 表只读。健康合同为 drama-youtube-writer-preflight-v3，shared-existing-account / application-table-allowlist / db_least_privilege=false；仅核验必要能力，不宣称全量 grant 审计。每次写前验证 TRIGGER 可见性和无 trigger/FK，旧健康合同拒绝。既有 DDL/v2 payload 与 UI 合同不变；下文专用账号/旧 v2 health 是历史。本轮专项 108/108，独立唯一完整回归及实机发布验收另记，不叠加历史批次。

## 2026-08-27 现行状态

用户最新明确要求仅在 ads_ai 新建三表；原表不写、不改、不复制。固定结构见 `deploy/drama-youtube-ads-ai-v2.sql`，完整所有权/最小权限/CREATE-only 门禁见 [新表合同](ads-ai-new-tables-20260827.md)。旧 c719 的恢复证明只保留历史，不绑定新 schema 或新候选。

## CPU SQLite

- `drama_material_job.outputs_json` 继续使用既有列，仅把四个布尔键归一为 `concat_video`、`no_bgm_video`、`cover_16x9`、`random_template_video`；历史错误键 `random_template` 只读归一，不作为新存储合同；不删旧列。
- 随机模板 recipe ledger 冻结 job、source、manifest、assets、参数、recipe SHA、完成结果 URL/SHA/profile，完成后不可改写。
- `drama_material_short_link` 对 `(job_id, material_kind)` 唯一；自增 `id` 是 gy 数字 namespace；content_id、target、short URL、wrapper SHA 与发布状态不可变。
- `drama_youtube_publish` 是发布、处理、评论与凭据身份的权威任务账本，含冻结模板/渲染描述、source identity、resumable 状态、独立 `comment_status`/`sync_status`、lease owner/generation 与 unknown 标志。
- `drama_youtube_sync_outbox` 以 `(entity_kind, external_id)` 唯一，状态、attempt、lease generation 独立；同步失败不得改写已确认的视频/评论事实。
- 内部 canary 复用既有 privacy_status/operation_id/attempt/event 字段，不另加 canary DDL。固定 operation `drama-hk-deploy-unlisted-20260827-shahrul-263`、app1479/channel263/account255/UCHJ1jFaYuW8g5EM7hM5pPpg、privacy_status=unlisted；只接受精确任务 claim。普通 HTTP/UI/worker/outbox 保持 public lane。
- session intent 在创建上传会话前提交；未保存会话/视频身份的已尝试任务 hold，不另建会话。已确认评论 ID 在同步 hold/reconcile 中保留；unknown 评论不自动重试。canary outbox 只在视频/评论均 published、unknown=0 且远端 fresh processed/succeeded/unlisted 核验后处理。

`ensure_storage()` 仅做幂等 additive 初始化/补列。内部 canary CLI 不调用它；先要求已准备好的真实 CPU ledger，status 使用 mode=ro。禁止 DROP、DELETE、反向迁移和历史重解释。

## 当前 MySQL：ads_ai 专用新表

新 `ads_youtube_videos` 与 `ads_youtube_publish_log` 保存完整发布字段，`ads_youtube_comments` 保存完整评论字段；所有表保存 payload_json、payload_sha256、canary_operation_id、创建时间。固定 InnoDB/utf8mb4_bin/所有权 COMMENT、精确唯一索引，无 trigger/FK。缺表才 CREATE，兼容表只复验，不改写历史。runtime 仅三表 SELECT/INSERT/UPDATE，健康合同 v2，禁止原库写入、旧字段映射和负数队列 ID。

## 历史 MySQL 方案（已停用，不作为执行合同）

三张 legacy 表均位于 `kunlunads_dev`：`ads_youtube_videos`、`ads_youtube_comments`、`ads_youtube_publish_log`。2026-08-27 一致性 snapshot 的实际行数为 244151、53、55105，共299309；2026-08-26 的近似行数仅属历史。生产迁移只增加 nullable ASCII/binary `drama_external_video_id`、`drama_external_comment_id`、`drama_external_publish_id` 与唯一索引，历史行保持 NULL，不回填或重解释。

生产目标仍固定集群 `cynosdbmysql-5kxxsre7`、主库 `101.32.56.53:63353`、schema 与账号名。当前受控路线在只读63350取得三表一致性快照，恢复到CPU127.0.0.1:23357独立MySQL5.7；数据/结构/索引守恒、首次/幂等迁移通过后机器生成 `table_snapshot_rehearsal` 的0600 evidence，绑定候选/五文件代码、manifest/inventory/report/契约SHA与48小时snapshot、4小时验证时效。此证据不等于集群灾备。一次性 migrator 仅三表 SELECT/INSERT/CREATE/ALTER，完成后销毁；长期 writer 仅三表 SELECT/INSERT/UPDATE，永不获得DDL。

统一 writer 只接收受控 `select/insert/update`、精确表白名单和实体级 exact payload；将发布事实映射到 legacy 必填列，并用三个 external ID 列实现数据库级幂等。`publish_id` 是正的 32-bit 整数，publish_log external ID 是规范十进制字符串；video/comment external ID 分别冻结为对应字符串 ID，三类 identity mismatch 均拒绝。legacy `ads_youtube_videos.queue_id` 与 `ads_youtube_publish_log.created_queue` 都写成 `-publish_id`：负数 namespace 经线上只读审计为未使用，可保持两表关联，又不会伪装成现有正数 `ads_created_queue` ID。单进程并发 1，先 select 再 insert；同 ID 同内容复用，不同内容冲突。`127.0.0.1:18837` RPC health 精确校验三表全部现行列的 type/NULL/default/charset/collation/extra、external 唯一索引和最终最小 grants；额外列、类型漂移或额外权限均关闭同步。客户端 token copy 为 root:root 0600，服务端同值 copy 与 MySQL writer JSON 为 `drama-youtube:drama-youtube` 0600；缺 executor、缺表、schema/grant 不匹配或非白名单操作均 fail closed。

canary 的 video/publish_log 允许且必须保持 unlisted，只在精确 app/channel 与固定 canary_operation_id marker 下通过验证；评论也校验同一固定 marker/channel。writer 从已验证 payload 读取 privacy_status，不硬编码 public；safe_log 带固定 marker/unlisted。鉴权 health 返回 `drama-youtube-writer-preflight-v1`、exact writer_identity、writable/schema_verified/indexes_verified 与 grant_fingerprint；CLI 在第一次 OAuth/上传前验证，不扩展 RPC 操作权限。

## 文件所有权

- gy wrapper root：`/mnt/data-disk/drama-youtube-short-links/s2l/youtube`，复用现有 X 域名/TLS server；三层目录必须为 `drama-youtube:drama-youtube` 0750，Nginx access ACL 为 r-x，最终目录 default ACL 令新文件只读，文件为 0640。`deploy/configure_drama_youtube_short_link_root.sh --check` 是上线门禁；owner 是文件系统写权限，不代表创建新域名或新证书。
- HK release：`/data/drama-synthesis-gpu/releases/<git_sha>` 不可变，`current` 原子切换；资产 root、work、results 分离。
