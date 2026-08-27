# 数据迁移与三表恢复演练（2026-08-27 现行）

## 当前状态与范围

最新 CPU 候选为 `40042f9692fbec58caa5abbf41af35e9aefb54bc`（CPU-only 模板目录）；本次未执行数据库操作。以下真实三表证据仍只绑定历史候选 `c719bebf72be900ec3853858dc53b36b83beffd2`，不能改绑新候选或 docs-only SHA。新候选正式 apply 前须按精确 SHA 和时效要求重验/更新新证据，不覆盖旧目录。

历史候选 c719beb 已独立代码 QA 并 GitHub push/readback。主代理已通过 SSH 完成真实三表一致性 snapshot 与 CPU 本机隔离 MySQL 5.7.44 恢复/迁移演练，结果 PASS；这只证明指定三表的可恢复性、数据/结构守恒与本次 additive migration 幂等，不是 Tencent API 云备份，更不是 CynosDB 全集群灾备已验证。

当前生产 `ads_aius` 对 `kunlunads_dev` 只有 SELECT/SHOW VIEW，没有合法 admin/migrator/writer；因此生产 DDL、writer/RPC 和真正 YouTube 测试仍 HOLD。演练成功不能绕过该权限门禁。全部支持操作仅通过 SSH，禁止腾讯云管理后台、不购买新云集群。

## CPU SQLite outputs_json

此路径与下面的统一 MySQL 门禁独立；不创建外部平台对象。

1. 停止新 drama job 写入并记录 Git SHA、DB 路径/大小/权限；确认绝对备份目标不存在且不等于源文件。
2. 运行不带 --apply 的 dry-run；以 mode=ro 打开 DB，校验精确列和每行 JSON，输出 rows/changes，不创建备份、不写数据。
3. 使用 `--apply --backup <new-absolute-path>`；先用 SQLite online backup API 生成一致性备份并 fsync，再以 BEGIN IMMEDIATE 单事务执行带 original-value predicate 的逐行更新。
4. 任一 JSON/schema/concurrency 错误整批 rollback，不跳过坏行。二次 dry-run 必须 changes=0，并执行 integrity check、四键抽样与任务输出读回。
5. 已有显式布尔优先；缺失的三个历史普通产物按已存结果 URL 推断；权威 random_template_video 缺失固定 false。旧错误 random_template 只作输入 fallback，迁移后只存 random_template_video，不以新任务默认重解释历史。

## 真实三表 snapshot 与隔离恢复证据

### 固定端点与隔离对象

| 对象 | 现行合同 |
| --- | --- |
| 生产集群/库 | cynosdbmysql-5kxxsre7 / kunlunads_dev |
| 只读快照源 | 101.32.56.53:63350；三表使用同一 READ ONLY 一致性事务 |
| 生产迁移端点 | 101.32.56.53:63353；仍需精确 migrator/可写主库与合法权限 |
| 演练入口 | CPU 127.0.0.1:23357；独立账号 drama_rehearsal；不是生产账号 |
| 本次 context | 20260827a11c0001 |
| 隔离 schema/container | drama_youtube_rehearsal_20260827a11c0001 / drama-youtube-rehearsal-20260827a11c0001 |
| 数据盘目录 | /mnt/data-disk/drama-youtube-rehearsal-20260827a11c0001/mysql |
| 私有 snapshot 目录 | /mnt/data-disk/drama-youtube-rehearsal-20260827a11c0001/snapshot |

只导出 ads_youtube_videos、ads_youtube_comments、ads_youtube_publish_log。源连接先验证 MySQL 5.7、只读实例、精确 legacy schema/InnoDB/主键，再在同一 REPEATABLE READ / WITH CONSISTENT SNAPSHOT / READ ONLY 事务内按 id 排序导出；UTC、SQL mode、schema/index、行数与行哈希写入 manifest。导出结束后关闭源连接，恢复阶段不能复用生产连接。

恢复前必须检查实际 Docker inspect：固定 digest 的 MySQL 5.7 镜像、bridge、唯一 127.0.0.1:23357→3306 端口映射、唯一专用数据盘 bind mount、空的独立 schema；目标 server_uuid 必须与源不同，hostname/端口需与被检查容器一致。禁止 host networking、非 loopback、生产 schema、额外挂载或混用已有库。脚本不会建账号、启动容器、DROP 或清空已有数据库。

数据与证据目录必须在真正挂载的 /mnt/data-disk 上、位于 Git 仓库之外，目录当前执行者所有且 0700，文件 exact 0600、regular、非 symlink、不能覆盖；最大 snapshot 2 GiB，检查至少 4 GiB/估算六倍的空间余量。快照包含私人业务行，不能提交 Git、上传报告或打印样本内容。

### 本次已通过的真实证据

候选 SHA：`c719bebf72be900ec3853858dc53b36b83beffd2`。视频/评论/日志分别 244151/53/55105 行，总计 299309 行；下列文件均在上面的私有 snapshot 目录，由主代理 SSH 实测回报：

| 文件 | SHA-256 |
| --- | --- |
| snapshot-manifest.json | 426685eda5041d332cde8f70ca724a7bbc3ae6038a0da6d02d1fabc2233f0603 |
| rehearsal-result.json | 0178a8b633c6433cffca4be32cdb4b5adfaa47e63bcaafb1398d847455d7d43b |
| backup-evidence.json | 36579d5ed7a2234d821638b3644c4b32ce024354cbdc136aa97b53dbc3fe9dec |

执行序列为恢复→dry-run（三表计划）→首次 apply→第二次 apply（空计划）→最终 dry-run（空计划）。恢复后、首次迁移后、二次迁移后，legacy 行数/rows SHA、完整 legacy schema SHA、原 indexes SHA 必须与源 snapshot 相等；三个新增 external-id 列在所有历史行上仍为 NULL，三唯一索引完整。只在全部通过且目标连接已关闭后生成 PASS report/evidence；失败保留私有部分产物供核查，不生成成功证明，也不自动清理。

### SHA、时效与代码绑定

`verification_source=table_snapshot_rehearsal` 的 evidence 不是手填 JSON。生产 apply 会重新验证同目录 manifest/report/逐表 schema 与 rows 文件；不接受只写一个 PASS 或复制示例文件。

- candidate_git_sha 必须精确匹配已 QA 的 40 位 SHA；导出/演练的 checkout HEAD 和五个候选代码文件必须干净一致。
- manifest 绑定 migration_contract_sha256、source_contract_sha256、每个候选文件 SHA、每表 rows/schema/index/inventory SHA；report/evidence 再绑定 candidate_code_sha256、snapshot_manifest_sha256、inventory_sha256 和 rehearsal_result_sha256。
- 五个候选文件为迁移脚本、三表演练脚本、unified_youtube_rpc.py、unified_youtube.py、core.py。变更这些文件或契约后不能把旧 evidence 改写成新候选 PASS。
- evidence 同时绑定 context、固定端口、容器 ID/镜像 digest、独立目标 UUID、四次迁移结果与三阶段投影。
- 快照完成距使用不超过 48 小时，最终验证距使用不超过 4 小时；各事件时间有序，未来时钟漂移最多 5 分钟。过期或任一哈希不符都阻断，不能手改时间戳续期。

### 可复现入口（仅说明，不在文档更新中执行）

从已核验候选 checkout 运行；凭据路径为占位，必须指向现有的当前执行者所有 0600 文件。context/目录不能与其他任务混用，已完成目录禁止覆盖。

```sh
python scripts/drama_youtube_three_table_rehearsal.py --export-snapshot \
  --source-credential-file "<只读源凭据绝对路径>" \
  --snapshot-dir /mnt/data-disk/drama-youtube-rehearsal-20260827a11c0001/snapshot \
  --candidate-git-sha c719bebf72be900ec3853858dc53b36b83beffd2 \
  --rehearsal-context 20260827a11c0001

python scripts/migrate_drama_youtube_unified_schema.py --rehearse-loopback \
  --credential-file "<本机隔离演练凭据绝对路径>" \
  --cluster-id cynosdbmysql-5kxxsre7 \
  --snapshot-dir /mnt/data-disk/drama-youtube-rehearsal-20260827a11c0001/snapshot \
  --snapshot-manifest-sha256 426685eda5041d332cde8f70ca724a7bbc3ae6038a0da6d02d1fabc2233f0603 \
  --candidate-git-sha c719bebf72be900ec3853858dc53b36b83beffd2 \
  --rehearsal-context 20260827a11c0001 --rehearsal-port 23357
```

本次目录已完成，上述命令是已走通入口的记录，不应在该目录再次执行导出/恢复。新演练须有新的受控 context/空目录、独立 schema/container/datadir，并重新绑定全部 SHA。

## 生产 unified MySQL additive migration（权限仍阻塞）

1. 由合法管理员创建一次性 drama_youtube_migrator@43.166.187.96，仅三张精确表 SELECT/INSERT/CREATE/ALTER；禁止 schema wildcard、DELETE/DROP/INDEX/UPDATE/GRANT OPTION 与额外 routine/proxy。凭据 root-owned 0600，例如 /etc/drama-youtube/migrator-db.json；长期 writer 不参与迁移。
2. 对固定主库执行 `--dry-run --cluster-id cynosdbmysql-5kxxsre7 --credential-file <绝对路径>`。先读回精确账号/grants/schema fingerprint，仅计划三个 nullable ASCII/binary external-id 列及唯一索引。
3. apply 必须同时传 `--backup-evidence-file <真实backup-evidence.json绝对路径> --candidate-git-sha c719bebf72be900ec3853858dc53b36b83beffd2`。重新验证上述证据、主库 @@read_only=0、精确账号/schema/cluster/grants 后才执行 ALGORITHM=INPLACE,LOCK=NONE，并读回每个列/索引。失败保留证据、关闭 sync，修复后幂等续跑，不反向 DDL。
4. 二次 dry-run 必须 complete=true、plan 为空；管理员撤销全部授权并销毁 migrator，按受控流程撤除一次性凭据。历史 external-id 全保持 NULL，不回填。
5. 创建从未持有 DDL 的长期 drama_youtube_writer@43.166.187.96，仅逐表 SELECT/INSERT/UPDATE；代码安装在 root:root 0755 的 `/opt/drama-youtube-unified-writer/releases/<candidate_git_sha>`。先 namei/test-r 验证服务用户可遍历/读代码，不复制或放宽凭据。
6. 用实际服务身份执行 runtime 验证：

```sh
runuser -u drama-youtube -- /usr/bin/python3 \
  /opt/drama-youtube-unified-writer/current/scripts/migrate_drama_youtube_unified_schema.py \
  --credential-file /etc/drama-youtube/writer-db.json \
  --cluster-id cynosdbmysql-5kxxsre7 --verify-runtime-writer
```

须通过全量 schema/index 和 USER/SCHEMA/TABLE/COLUMN/SHOW GRANTS 的精确权限闭包；MySQL 5.7 不查询不存在的 ROUTINE_PRIVILEGES，routine/proxy/未知授权由 SHOW GRANTS 拒绝。root 直接读取服务用户 exact-0600 凭据时，脚本以 `writer database credential file is invalid` 拒绝；这是预期门禁，禁止更改 owner/mode 绕过。随后鉴权 RPC health 还须返回版本化 contract、精确 writer_identity、可写/schema/index 验证标记和 grant_fingerprint，才允许内部 canary 的第一次 OAuth refresh/upload。

## 回滚与历史说明

常规回滚关闭 unified sync/live、停 loopback writer并回退应用 SHA；保留 nullable 列/唯一索引、历史 NULL、短链与发布/outbox 状态。不得用共享库整库恢复回退此次应用，不得 DELETE/DROP/反向 DDL。

旧 `deploy/drama-youtube-backup-evidence.example.json` 展示的是保留的 tencent_cynosdb_api 证据分支，不是当前 SSH 三表演练模板，也不是必须走的操作路径。当前实际证据由脚本生成并标明 table_snapshot_rehearsal，不伪造 API backup ID，不进入腾讯云管理后台。全集群灾难恢复未验证、不在本次授权范围；若另有灾难恢复需求，需另行授权与独立方案。
