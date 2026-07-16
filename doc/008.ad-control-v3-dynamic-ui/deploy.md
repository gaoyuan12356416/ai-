# 自动调控 V3 部署与回滚

## 1. 当前状态

本文是待执行发布 Runbook。截止文档收口时：本地 V3 132/132 和本地 Playwright 已通过；GitHub 提交/推送、生产 DDL、代码 overlay、服务重启、线上浏览器、真实手动 observe 和 V2 发布后回归均未完成。

本轮生产目标只到：**FB 动态两页 + 配置 + 手动 observe**。不得创建/启用 V3 timer，不得 enable 规则，不得调用 Meta 写接口。

## 2. 已核实生产基线

- live root：`/root/drama_material_service`，不是 Git checkout。
- API service：`drama-material-api.service`，Python 3.9。
- 当前生产 `app.py` SHA-256（只读核实时间点）：`7ed60179abc83880d41f2547ed19e3591136dca693c776df5d5ecfe6a2546b49`，与 source commit `2b52bc8d06b8a36a473dad8916012570ee28c15b` 对应；发布瞬间必须重新核对。
- Nginx 已有 `location /api/ad-control/` 代理到 8787；V3 无 Nginx 配置变更。
- 数据盘：`/mnt/data-disk`，`/dev/vdb1`；V3 根为 `/mnt/data-disk/ai-ad-control-v3`。
- MySQL host：`101.32.56.53`；reader 63350，writer 63353；`ads_ai` 当前基线需在执行 DDL 前重新导出。
- 本地源 bundle 备份：`D:\codex\backups\ad-control-v3-prechange-20260716-101213-2b52bc8\source.bundle`，SHA-256 `e0f361ba2a0c062019bf51eff89964188338bcfd0694f48c543e17d31852cdc7`。

以上是历史只读证据，不替代发布瞬间复核。

## 3. 发布边界

- GitHub-first：本地最终验证 -> commit/push -> 服务器 fetch 精确 commit -> staging -> 生产。
- 不 `git pull` 覆盖 live root，不整份复制 checkout `app.py`，不在线热改。
- `deploy/apply_ad_control_v3.py` 只安装 `features/ad_control_v3/**` 的允许后缀、`scripts/ad_control_v3_runner.py` 和经审查的 `app.py` 纯新增 dispatcher。
- SQL、systemd、navigation、环境配置均不由 overlay 部署器安装，分别做独立检查点。
- 旧 V2 静态页、feature、SQLite、runner、cron 和 `ads_ai.ad_control_action_log` 零 DDL/DML。

## 4. 数据盘准备

所有 V3 运行数据、配置、缓存、staging 和发布备份放在数据盘：

```text
/mnt/data-disk/ai-ad-control-v3/
├─ config/
├─ snapshots/
├─ logs/
├─ run/
├─ spool/
├─ tmp/
├─ exports/
├─ cache/
├─ staging/
└─ backups/
```

发布前只读验证：

```bash
realpath /mnt/data-disk/ai-ad-control-v3
findmnt -T /mnt/data-disk/ai-ad-control-v3
df -hT /mnt/data-disk/ai-ad-control-v3
stat -c '%d %U %G %a %n' / /mnt/data-disk /mnt/data-disk/ai-ad-control-v3
```

根目录/子目录 mode `0700`，配置与快照 `0600`。路径位于根分区、`/root`、应用 checkout 或经过 symlink 时，V3 必须返回 `unsafe_data_root`，不能回退到系统盘。

## 5. 环境配置

推荐创建 `/mnt/data-disk/ai-ad-control-v3/config/runtime.env`，mode 0600，再以独立 systemd drop-in `EnvironmentFile=-.../runtime.env` 注入。不要把密码打印到终端输出或提交 Git。

必填：

```text
AD_CONTROL_V3_DATA_ROOT=/mnt/data-disk/ai-ad-control-v3
AD_CONTROL_V3_SOURCE_READER_MYSQL_HOST=101.32.56.53
AD_CONTROL_V3_SOURCE_READER_MYSQL_PORT=63350
AD_CONTROL_V3_SOURCE_READER_MYSQL_USER=...
AD_CONTROL_V3_SOURCE_READER_MYSQL_PASSWORD=...
AD_CONTROL_V3_STORE_READER_MYSQL_HOST=101.32.56.53
AD_CONTROL_V3_STORE_READER_MYSQL_PORT=63350
AD_CONTROL_V3_STORE_READER_MYSQL_USER=...
AD_CONTROL_V3_STORE_READER_MYSQL_PASSWORD=...
AD_CONTROL_V3_STORE_WRITER_MYSQL_HOST=101.32.56.53
AD_CONTROL_V3_STORE_WRITER_MYSQL_PORT=63353
AD_CONTROL_V3_STORE_WRITER_MYSQL_USER=...
AD_CONTROL_V3_STORE_WRITER_MYSQL_PASSWORD=...
```

可选安全参数：

```text
AD_CONTROL_V3_MYSQL_CONNECT_TIMEOUT_SECONDS=3
AD_CONTROL_V3_MYSQL_IO_TIMEOUT_SECONDS=5
AD_CONTROL_V3_SCAN_CONCURRENCY=1
AD_CONTROL_V3_PREVIEW_TTL_SECONDS=1800
AD_CONTROL_V3_SNAPSHOT_MAX_RAW_BYTES=67108864
AD_CONTROL_V3_SNAPSHOT_MAX_GZIP_BYTES=33554432
AD_CONTROL_V3_DATA_MIN_FREE_BYTES=1073741824
AD_CONTROL_V3_RUNNER_ENABLED=0
AD_CONTROL_V3_RUNNER_OBSERVE_RELEASED=0
```

不要在生产设置 `AD_CONTROL_V3_ALLOW_NONSTANDARD_MYSQL_HOST`。即使误设两个 runner 开关，当前 runner 仍返回 `runner_scheduler_not_configured`；不安装 timer 是第二道门禁。

## 6. 数据库独立检查点

### 6.1 迁移前基线

使用 63350 只读连接保存：MySQL 版本、`SHOW TABLES FROM ads_ai`、相关表 `SHOW CREATE TABLE`、旧 `ad_control_action_log` 行数/schema hash。保存三个 SQL 文件的 SHA-256。

写端执行前必须验证 host/port/database allowlist；除事务/会话安全检查外，不在 63353 做泛查询。

### 6.2 DDL 与 seed

1. 在临时 MySQL 5.7 环境验证 `001_create_ad_control_v3_tables.sql`、`002_seed_fb_short_drama_products.sql` 和空表 rollback。
2. 审批后在 63353 执行 `001`，只允许创建八张 `ads_ai.ad_control_v3_*` 表。
3. 在 63350 等待复制可见并逐表回读 `SHOW CREATE TABLE`，核对字段、索引、字符集、八表数量；四处 `product_value/canonical_product` 必须显式为 `utf8mb4_bin`。
4. 再在 63353 执行 `002`；63350 回读 15 个精确 FB 短剧产品。
5. 确认 `kunlunads_dev`、旧 SQLite 和 `ads_ai.ad_control_action_log` 无写入。

本期 SQL 不创建 copied `created_data`、lineage 或 intent 表。

### 6.3 数据库回滚

- 八表为空且从未产生真实配置/审计：经审批执行 `900_rollback_empty_v3_tables.sql`；脚本必须先拒绝任何非空表。
- 任一表已有真实配置、Preview 或日志：禁止 DROP，只关闭 V3 入口/写入并保留审计。

## 7. GitHub staging

```bash
export SOURCE_COMMIT='2b52bc8d06b8a36a473dad8916012570ee28c15b'
export TARGET_COMMIT='<最终已评审并 push 的精确 commit>'
export REPO='/mnt/data-disk/ai-ad-control-v3/staging/ai-drama-material-service'
export LIVE_ROOT='/root/drama_material_service'
export BACKUP_ROOT='/mnt/data-disk/ai-ad-control-v3/backups'
```

```bash
git -C "$REPO" fetch --prune origin
git -C "$REPO" cat-file -e "${SOURCE_COMMIT}^{commit}"
git -C "$REPO" cat-file -e "${TARGET_COMMIT}^{commit}"
git -C "$REPO" diff --check "$SOURCE_COMMIT" "$TARGET_COMMIT"
git -C "$REPO" show --stat --oneline "$TARGET_COMMIT"
```

发布人必须重新计算 live `app.py` hash。若不再与 SOURCE_COMMIT blob 一致，立即停止，基于现场新源重做 overlay、评审和全量测试。

## 8. Exact-source overlay

### 8.1 只读预检

```bash
python3 "$REPO/deploy/apply_ad_control_v3.py" \
  --root "$LIVE_ROOT" \
  --repo "$REPO" \
  --source-commit "$SOURCE_COMMIT" \
  --target-commit "$TARGET_COMMIT" \
  --backup-dir "$BACKUP_ROOT" \
  --lock-file "$LIVE_ROOT/.deployment.lock" \
  --check
```

预期 `would_change` 或 `unchanged`。任何 source/target/live drift、未知 runtime 文件、symlink、无关 app 差异、缺少传递依赖或非数据盘 backup 都必须停止，且 `--check` 不写任何文件。

### 8.2 正式应用

先完成生产全量 checkpoint：live app、service unit/drop-in、runtime.env 的加密/受限备份、navigation、Nginx、cron、V2 SQLite online backup/integrity/hash、旧 V2 runtime hashes、ads_ai schema/data baseline。

```bash
python3 "$REPO/deploy/apply_ad_control_v3.py" \
  --root "$LIVE_ROOT" \
  --repo "$REPO" \
  --source-commit "$SOURCE_COMMIT" \
  --target-commit "$TARGET_COMMIT" \
  --backup-dir "$BACKUP_ROOT" \
  --lock-file "$LIVE_ROOT/.deployment.lock"
```

部署器在同一锁内先安装 runtime、最后安装 app；中途失败自动逆序恢复/删除新文件。第二次 apply 必须 `unchanged`，不得新建重复 backup。

## 9. Staging 与服务验证

```bash
cd "$LIVE_ROOT"
PYTHONPYCACHEPREFIX=/mnt/data-disk/ai-ad-control-v3/cache/pycache \
  python3 -m py_compile app.py scripts/ad_control_v3_runner.py
PYTHONPYCACHEPREFIX=/mnt/data-disk/ai-ad-control-v3/cache/pycache \
  python3 -m compileall -q features/ad_control_v3
node --check features/ad_control_v3/assets/app.js
nginx -t
```

在 staging 精确 commit 上运行 132 条 V3 测试。只重启 `drama-material-api.service`；记录重启前后 PID、启动时间和 journal 游标，不重启无关 worker。

## 10. Route dark 与线上验收

### 10.1 Route dark

- 导航暂不发布；V3 runner/timer 不存在或 disabled/stopped。
- 无 cookie 页面拒绝；API Token 访问页面/asset 返回 `cookie_auth_required`；无模块权限 403。
- 有权限 cookie 两个动态页 200、no-store；assets allowlist 正常，未知路径 JSON 404。
- `/meta` 显示 FB enabled、TT/live/copy/scheduler disabled。
- 旧 V2 页面/API/自然 tick 正常。

### 10.2 手动 observe

按 Campaign -> Ad Set -> Ad 分别执行，每次只选一个 optimizer、一个产品、1 天窗口：

- 核对范围 SQL 的 platform/product/dt/optimizer 前置条件与超时；
- 核对产品、optimizer、时区、对象身份、歧义和规则目标；
- 核对 Preview/Execution/Targets 写入八表，快照位于数据盘且 hash 可读；
- 明确记录 Token lookup、Graph GET/POST/copy、Meta write 均为 0；
- 不点击/调用 enable，不运行 V3 runner。

### 10.3 Navigation 独立发布

生产页面/权限和手动 observe 通过后，使用独立 `apply_ad_control_v3_navigation.py`。脚本从 staging 当前精确 `HEAD` Git blob 读取 `ad_control_v3` 分组，只键级合并两个动态链接，拒绝 checkout drift、现场同 key 冲突、symlink、错误权限和非数据盘备份；专项自动化 13/13。

staging 必须 detached 到 `TARGET_COMMIT`，分别预检 service/static 与 Nginx 两份现场文件：

```bash
python3 "$REPO/deploy/apply_ad_control_v3_navigation.py" \
  --repo-root "$REPO" \
  --live-target /root/drama_material_service/static/navigation.json \
  --backup-root /mnt/data-disk/ai-ad-control-v3/backups/navigation-service \
  --check

python3 "$REPO/deploy/apply_ad_control_v3_navigation.py" \
  --repo-root "$REPO" \
  --live-target /usr/share/nginx/html/navigation.json \
  --backup-root /mnt/data-disk/ai-ad-control-v3/backups/navigation-nginx \
  --check
```

两项均为 `would_change`/`unchanged` 且人工确认现场其他组保留后，去掉 `--check` 分别 apply，记录每个 checkpoint。重复 apply 必须 `unchanged`。回滚按 checkpoint 且先 `--check`：

```bash
python3 "$REPO/deploy/apply_ad_control_v3_navigation.py" \
  --live-target '<该 checkpoint 对应的原 live target>' \
  --rollback '<checkpoint.json>' --check
```

确认 `would_rollback` 后去掉 `--check`。任何 installed hash 或 backup hash 漂移都必须拒绝回滚覆盖。

## 11. V2 零影响验收

- 发布前后旧静态页、feature、runner hash；
- `/api/ad-control/*` 契约、owner 隔离、旧 action log；
- V2 SQLite online backup/integrity/schema/row hash/enabled/emergency；
- ad-control cron 唯一行、日志、锁和至少一轮自然 tick；
- V3 路径外不 import `features.ad_control_v3`，无 V3 DB/数据盘 I/O。

任何不一致先停止 V3 入口，不能用“132 条 V3 测试通过”代替 V2 生产证据。

## 12. 回滚

### 12.1 Overlay 内失败

部署器自动逆序恢复 app/runtime，并删除本次新文件。命令结束后仍需重算 hash，不因异常提示就假设恢复成功。

### 12.2 已完成 release

确认当前 live 仍逐字节等于 TARGET_COMMIT 后：

```bash
python3 "$REPO/deploy/apply_ad_control_v3.py" \
  --root "$LIVE_ROOT" \
  --repo "$REPO" \
  --source-commit "$SOURCE_COMMIT" \
  --target-commit "$TARGET_COMMIT" \
  --backup-dir "$BACKUP_ROOT" \
  --lock-file "$LIVE_ROOT/.deployment.lock" \
  --rollback --check

python3 "$REPO/deploy/apply_ad_control_v3.py" \
  --root "$LIVE_ROOT" \
  --repo "$REPO" \
  --source-commit "$SOURCE_COMMIT" \
  --target-commit "$TARGET_COMMIT" \
  --backup-dir "$BACKUP_ROOT" \
  --lock-file "$LIVE_ROOT/.deployment.lock" \
  --rollback
```

目标有任何外部漂移时 rollback 必须拒绝覆盖。回滚 navigation/systemd drop-in 分别使用其独立 checkpoint。

### 12.3 数据与快照

真实记录存在后不 DROP 表。代码回滚不删除快照；当前没有清理器，孤儿快照保留待审计。本期 V3 无 Meta 写，因此无需恢复 Meta 状态。

## 13. 发布证据清单

最终报告必须包含：source/target commit、live app/runtime hash、数据盘 checkpoint、DDL/seed hash 与 schema 回读、service/journal 时间点、线上 HTTP/浏览器结果、三层 observe 零外部写、V2 前后对比、navigation before/after/rollback 信息。缺任一 P0 证据，发布建议保持“不可放量”。
