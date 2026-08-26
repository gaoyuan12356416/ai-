# 数据与所有权合同

## CPU SQLite

- `drama_material_job.outputs_json` 继续使用既有列，仅把四个布尔键归一为 `concat_video`、`no_bgm_video`、`cover_16x9`、`random_template_video`；历史错误键 `random_template` 只读归一，不作为新存储合同；不删旧列。
- 随机模板 recipe ledger 冻结 job、source、manifest、assets、参数、recipe SHA、完成结果 URL/SHA/profile，完成后不可改写。
- `drama_material_short_link` 对 `(job_id, material_kind)` 唯一；自增 `id` 是 gy 数字 namespace；content_id、target、short URL、wrapper SHA 与发布状态不可变。
- `drama_youtube_publish` 是发布、处理、评论与凭据身份的权威任务账本，含冻结模板/渲染描述、source identity、resumable 状态、独立 `comment_status`/`sync_status`、lease owner/generation 与 unknown 标志。
- `drama_youtube_sync_outbox` 以 `(entity_kind, external_id)` 唯一，状态、attempt、lease generation 独立；同步失败不得改写已确认的视频/评论事实。

`ensure_storage()` 仅 `CREATE TABLE/INDEX IF NOT EXISTS` 和经 `PRAGMA table_info` 判定后的 additive `ALTER TABLE ... ADD COLUMN lease_generation ... DEFAULT 0`。禁止 DROP、DELETE、反向迁移和历史重解释。

## 外部统一 MySQL

目标表精确为 `ads_youtube_videos`、`ads_youtube_comments`、`ads_youtube_publish_log`。当前只读证据确认三表尚不存在，因此候选不含 DDL。统一 writer 只接收受控 `select/insert/update`、精确表白名单和实体级 exact payload；`publish_id` 在 payload 中是正整数，在 publish_log external ID 中是规范十进制字符串，video/comment external ID 分别冻结为对应字符串 ID，三类 identity mismatch 均拒绝。单进程并发 1，先 select 再 insert/update，结果必须声明 idempotent success。worker 的 executor 由受控 RPC env factory 构造，RPC 端负责表 schema/外部 ID 唯一约束；credential 只在 0600 文件。缺 executor、缺表、schema 不匹配或非白名单操作均 fail closed，claimed outbox 即使 payload JSON/合同无效也由 owner+generation fenced 写为 failed，禁止宣称同步成功或记录原 payload。

## 文件所有权

- gy wrapper root：`/mnt/data-disk/drama-youtube-short-links/s2l/youtube`，必须由应用专用 owner 独占写；nginx 只读。
- HK release：`/data/drama-synthesis-gpu/releases/<git_sha>` 不可变，`current` 原子切换；资产 root、work、results 分离。
