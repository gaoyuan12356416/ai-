# 数据与所有权合同

## CPU SQLite

- `drama_material_job.outputs_json` 继续使用既有列，仅把四个布尔键归一为 `concat_video`、`no_bgm_video`、`cover_16x9`、`random_template_video`；历史错误键 `random_template` 只读归一，不作为新存储合同；不删旧列。
- 随机模板 recipe ledger 冻结 job、source、manifest、assets、参数、recipe SHA、完成结果 URL/SHA/profile，完成后不可改写。
- `drama_material_short_link` 对 `(job_id, material_kind)` 唯一；自增 `id` 是 gy 数字 namespace；content_id、target、short URL、wrapper SHA 与发布状态不可变。
- `drama_youtube_publish` 是发布、处理、评论与凭据身份的权威任务账本，含冻结模板/渲染描述、source identity、resumable 状态、独立 `comment_status`/`sync_status`、lease owner/generation 与 unknown 标志。
- `drama_youtube_sync_outbox` 以 `(entity_kind, external_id)` 唯一，状态、attempt、lease generation 独立；同步失败不得改写已确认的视频/评论事实。

`ensure_storage()` 仅 `CREATE TABLE/INDEX IF NOT EXISTS` 和经 `PRAGMA table_info` 判定后的 additive `ALTER TABLE ... ADD COLUMN lease_generation ... DEFAULT 0`。禁止 DROP、DELETE、反向迁移和历史重解释。

## 外部统一 MySQL

2026-08-26 只读实查确认三张 legacy 表均位于 `kunlunads_dev`：`ads_youtube_videos`、`ads_youtube_comments`、`ads_youtube_publish_log`，现有规模约 24.3 万、53、5.5 万行。运行前只做 additive migration：分别增加 nullable ASCII/binary `drama_external_video_id`、`drama_external_comment_id`、`drama_external_publish_id` 与唯一索引；历史行保持 NULL，禁止回填或重解释。迁移脚本固定集群 `cynosdbmysql-5kxxsre7`、主库 `101.32.56.53:63353`、schema 与账号名，要求 48 小时内成功备份、API 读回和恢复演练 PASS 的 0600 evidence 文件，支持 dry-run/幂等重跑。一次性 `drama_youtube_migrator@43.166.187.96` 仅在迁移窗口持有三表级 SELECT/INSERT/CREATE/ALTER，完成后二次 dry-run并销毁；长期 `drama_youtube_writer@43.166.187.96` 从未获得 DDL，最终仅留三表级 SELECT/INSERT/UPDATE。

统一 writer 只接收受控 `select/insert/update`、精确表白名单和实体级 exact payload；将发布事实映射到 legacy 必填列，并用三个 external ID 列实现数据库级幂等。`publish_id` 是正的 32-bit 整数，publish_log external ID 是规范十进制字符串；video/comment external ID 分别冻结为对应字符串 ID，三类 identity mismatch 均拒绝。legacy `ads_youtube_videos.queue_id` 与 `ads_youtube_publish_log.created_queue` 都写成 `-publish_id`：负数 namespace 经线上只读审计为未使用，可保持两表关联，又不会伪装成现有正数 `ads_created_queue` ID。单进程并发 1，先 select 再 insert；同 ID 同内容复用，不同内容冲突。`127.0.0.1:18837` RPC health 精确校验三表全部现行列的 type/NULL/default/charset/collation/extra、external 唯一索引和最终最小 grants；额外列、类型漂移或额外权限均关闭同步。客户端 token copy 为 root:root 0600，服务端同值 copy 与 MySQL writer JSON 为 `drama-youtube:drama-youtube` 0600；缺 executor、缺表、schema/grant 不匹配或非白名单操作均 fail closed。

## 文件所有权

- gy wrapper root：`/mnt/data-disk/drama-youtube-short-links/s2l/youtube`，复用现有 X 域名/TLS server；三层目录必须为 `drama-youtube:drama-youtube` 0750，Nginx access ACL 为 r-x，最终目录 default ACL 令新文件只读，文件为 0640。`deploy/configure_drama_youtube_short_link_root.sh --check` 是上线门禁；owner 是文件系统写权限，不代表创建新域名或新证书。
- HK release：`/data/drama-synthesis-gpu/releases/<git_sha>` 不可变，`current` 原子切换；资产 root、work、results 分离。
