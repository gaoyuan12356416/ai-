# 历史 outputs_json 迁移

迁移分为 CPU SQLite 与统一 MySQL 两条独立门禁；都不触碰短链文件或外部平台。

1. 停止新 drama job 写入并记录当前 Git SHA、DB 路径/大小/权限；确认绝对备份目标不存在且不等于源文件。
2. 运行不带 `--apply` 的 dry-run；脚本以 `mode=ro` 打开 DB，校验精确列和每行 JSON，输出 rows/changes，不创建备份、不写数据。
3. 使用 `--apply --backup <new-absolute-path>`；先用 SQLite online backup API 生成一致性备份并 fsync，再以 `BEGIN IMMEDIATE` 单事务执行带 original-value predicate 的逐行更新。
4. 任一 JSON/schema/concurrency 错误整批 rollback；不得跳过坏行。成功后二次 dry-run 必须 `changes=0`，并执行 integrity check、四键抽样与任务输出读回。
5. 归一规则：已有显式布尔优先；缺失的三个历史普通产物按已存结果 URL 推断；权威 `random_template_video` 缺失固定 false。历史错误 `random_template` 仅作为输入 fallback，迁移后只存 `random_template_video`，绝不采用新任务默认重解释历史。

## 统一 MySQL additive migration

1. 目标固定为 cluster `cynosdbmysql-5kxxsre7`、主库 `101.32.56.53:63353`、schema `kunlunads_dev` 和三张现有 legacy 表。由 Tencent CynosDB API 读回 48 小时内 `SUCCESS` 的 cluster backup，并恢复到一个 ID 不等于生产 cluster 的隔离实例。必须在该恢复实例用最终候选 Git SHA 对应的脚本完成 dry-run、apply、二次 dry-run和 48 列/3 唯一索引读回。将脱敏演练结果保存为文件并计算 SHA-256；按 `deploy/drama-youtube-backup-evidence.example.json` 的 exact keys 写入 root-owned 0600 evidence JSON，其中 `restore_instance_id`、`migration_contract_sha256`、`candidate_git_sha` 和 `rehearsal_result_sha256` 必须分别绑定恢复实例、脚本内置迁移契约、40 位小写候选 Git SHA 和演练结果文件。evidence 不得靠手填 PASS 代替恢复演练，也不得包含数据库密码。
2. 创建一次性 `drama_youtube_migrator@43.166.187.96`，仅对三张精确表授予 SELECT/INSERT/CREATE/ALTER，无 schema wildcard、DELETE/DROP/INDEX/UPDATE/GRANT OPTION；凭据为 root-owned 0600 `/etc/drama-youtube/migrator-db.json`。长期 writer 不参与迁移且永不获得 DDL。
3. 运行 `scripts/migrate_drama_youtube_unified_schema.py --dry-run --cluster-id cynosdbmysql-5kxxsre7 --credential-file /etc/drama-youtube/migrator-db.json`；脚本先读回精确账号/grants/legacy schema fingerprint，只允许计划三个 nullable external-id 列与唯一索引。
4. 使用 `--apply --backup-evidence-file <absolute-0600-json> --candidate-git-sha <40位小写候选SHA>` 执行；两个 SHA 必须与 evidence 和脚本内置 contract 完全一致。脚本要求主库 `@@read_only=0`、精确账号/schema/cluster/grants，使用 `ALGORITHM=INPLACE,LOCK=NONE`，并读回每个列/索引。失败后关闭 sync、保留证据，修正后幂等续跑，不做反向 DDL。
5. 二次 dry-run 必须 `complete=true`、plan 为空；随后由管理员撤销全部授权并销毁 migrator，删除本地 migrator 凭据。创建长期 writer，逐表只授予 SELECT/INSERT/UPDATE。候选代码固定安装为 root:root 0755 的 `/opt/drama-youtube-unified-writer/releases/<candidate_git_sha>`，`current` 只指向该目录；`/opt`、项目目录、`releases` 和候选目录都必须允许 `drama-youtube` 遍历，代码可读，但 `/etc/drama-youtube` 以外不得复制凭据。
6. 先执行 `namei -l /opt/drama-youtube-unified-writer/current/scripts/migrate_drama_youtube_unified_schema.py`，再执行 `runuser -u drama-youtube -- test -r /opt/drama-youtube-unified-writer/current/scripts/migrate_drama_youtube_unified_schema.py`。最终 runtime 验证必须原样使用下面的身份边界：

```sh
runuser -u drama-youtube -- /usr/bin/python3 \
  /opt/drama-youtube-unified-writer/current/scripts/migrate_drama_youtube_unified_schema.py \
  --credential-file /etc/drama-youtube/writer-db.json \
  --cluster-id cynosdbmysql-5kxxsre7 \
  --verify-runtime-writer
```

该命令必须读回精确账号、三表全量 schema fingerprint、唯一索引以及 USER/SCHEMA/TABLE/COLUMN/ROUTINE/PROXY 的最小权限闭包，任何额外权限均失败。若错误地以 root 直接读取 `drama-youtube:drama-youtube` 0600 凭据，脚本应以 `writer database credential file is invalid` 阻断；这是预期门禁，禁止通过放宽文件权限或改 owner 绕过。历史 external-id 全部保持 NULL，不回填。

常规回滚：关闭 unified sync、停 loopback writer并回退应用 SHA；保留 nullable 列、唯一索引和历史 NULL，不反向 DDL，不恢复共享生产集群。云备份仅用于数据损坏等灾难恢复，必须另行审批并优先恢复到新集群验证，严禁用整库恢复回退无关生产写入。
