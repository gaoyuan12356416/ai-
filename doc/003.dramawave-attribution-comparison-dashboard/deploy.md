# 部署文档

## 1. 发布状态回填

2026-08-06 已完成的首次生产发布是 D7/D30 历史基线。下表是该版本的不可变发布证据，不代表 D10 已发布。2026-08-07 用户已批准 D7/D10 看板起点为 `2026-08-01`；代码边界已修改，生产 bootstrap、切换和验收尚未执行。

| D30 历史项目 | 实际值 |
| --- | --- |
| GitHub 目标 commit | `e92f2aef417ce47cabfb6e3ae2056d96ad7f9894` |
| 发布分支 / PR | `codex/dramawave-attribution-compare-20260806`；未创建 PR |
| 发布主机 | `43.166.187.96` |
| 发布时间（Asia/Shanghai） | `2026-08-06 21:10` |
| 发布 release 路径 | `/opt/dramawave-attribution-comparison/releases/e92f2aef417ce47cabfb6e3ae2056d96ad7f9894` |
| 发布前 `current` 指向 | 不存在（首次上线） |
| 发布前备份路径 | `/mnt/data-disk/dramawave-attribution-comparison/backups/20260806-190048-pre-9b1ae58fb140` |
| 首次 bootstrap 版本 | `20260806T130131Z-1e2f1adf`；`920,751` facts |
| 首次 bootstrap 日期范围 | `2026-07-29`～`2026-08-06` |
| 第一轮自然 timer 时间及版本 | `2026-08-06 21:22:00`～`21:27:01`；推进至 `20260806T132547Z-d44a154d` |
| 回滚点 | 首次上线无旧 release；停用并移除新 units/Nginx include，保留 SQLite；配置证据使用上述备份目录 |

### 1.1 D10 当前门禁

| 项目 | 当前结论 |
| --- | --- |
| 目标新源 | `kunlunads_dev.ads_app_revenues_10d` |
| schema/index | 已只读核验，与 D30 相同 |
| D10 本地自动化 | 边界调整后 `63/63` 通过；不等于候选/生产验收 |
| 当前最早 D10 日期 | `2026-08-01` |
| 批准业务起点 | `2026-08-01` |
| 日期决策 | 用户已明确批准从 8/1 开始；代码/测试边界已同步 |
| 生产状态 | 历史 D30 继续服务；D10 未切换 |

生产 bootstrap 只允许在边界提交已推送、D10 逐日源覆盖通过且全新候选路径确认不存在后执行。目标 commit 的 `MIN_DATE`、前端 `FALLBACK_MIN_DATE` 和 `BOOTSTRAP_START` 必须全部为 `2026-08-01`；D30 的 2026-08-06 发布数据只能作为容量、性能与回滚基线。

### 1.2 D10 独立数据库与原子切换原则

D10 不在历史 D30 SQLite 上做原地迁移。使用三个明确对象：

| 对象 | 路径/约束 |
| --- | --- |
| 历史 D30 live/回滚库 | `/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3`，原样保留 |
| 全新 D10 候选库 | `/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard-d10-<shortsha>.sqlite3`，发布前必须不存在 |
| 活动数据库指针 | `dashboard.env` 中的 `DRAMAWAVE_ATTRIBUTION_DB_PATH`；通过同目录临时文件 + `mv -Tf` 原子替换，禁止复制候选库覆盖 live 文件 |

D10 候选库第一次成功发布版本时，必须在同一 SQLite 事务中包含以下权威语义标记：

```text
cache_meta.comparison_window = "D10"
cache_meta.new_attribution_source = "kunlunads_dev.ads_app_revenues_10d"
cache_meta.data_version = <非空版本>
cache_meta.rollup_version = cache_meta.data_version
```

D10 Web 和 refresh 在任何普通 SQLite 打开前，都必须先用 `mode=ro&immutable=1` 校验 D10 结构签名及上述标记。checkpointed 历史 D30 库缺少 D10 列或标记不匹配时立即失败关闭；该粗门禁不能初始化 schema、切换 journal、创建 `-wal` / `-shm` 或写入任何字节。粗门禁通过后，必须再用普通只读、WAL-aware 连接复核同一合同，防止未 checkpoint 的已提交 WAL 篡改绕过检查；只有二次检查通过，refresh 才可 writable open、Web 才可启动。正常 API 也保持非 immutable，以便观察在线 WAL 更新。只有“从未存在”的新候选路径可以作为空 D10 库 bootstrap。

## 2. 固定拓扑

| 项目 | 固定值 |
| --- | --- |
| 代码发布根目录 | `/opt/dramawave-attribution-comparison` |
| 当前版本软链接 | `/opt/dramawave-attribution-comparison/current` |
| Web 监听 | `127.0.0.1:8832` |
| 公网页面 | `/reports/dramawave-attribution-comparison/` |
| 历史 D30 SQLite | `/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3`；D10 切换后仍保留作回滚 |
| D10 候选 SQLite | `/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard-d10-<shortsha>.sqlite3`；每次候选使用新路径 |
| 活动 SQLite 选择 | `dashboard.env` 的 `DRAMAWAVE_ATTRIBUTION_DB_PATH`；只允许原子替换环境文件 |
| 独立环境文件 | `/mnt/data-disk/dramawave-attribution-comparison/dashboard.env` |
| 复用的 MySQL 凭据环境文件 | `/root/drama_material_service/.env`，仅 refresh oneshot 读取 |
| Web unit | `dramawave-attribution-comparison.service` |
| 刷新 unit | `dramawave-attribution-comparison-refresh.service` |
| 刷新 timer | `dramawave-attribution-comparison-refresh.timer` |
| Nginx 配置 | `/etc/nginx/default.d/dramawave-attribution-comparison.conf` |
| 数据盘 UUID | `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8` |

Web 进程只读取 SQLite，不应拥有 `ADMIN_MAPPING_MYSQL_*` 凭据。refresh oneshot 才连接源库，并在代码中同时强制：

- MySQL 端口必须为 `63350`。
- 建连后 `SELECT @@read_only` 必须为 `1`。
- SQLite 路径必须位于 `/mnt/data-disk`，所在挂载 UUID 必须完全匹配上表。
- D10 发布时必须校验 `comparison_window=D10`、`new_attribution_source=kunlunads_dev.ads_app_revenues_10d` 和 D10 结构签名；不匹配时 Web/刷新均失败关闭。
- 同一时刻只能有一个 refresh；锁文件为缓存目录下的 `refresh.lock`。

不要使用 `--skip-mount-check` 部署或运行生产 Web/刷新进程。该参数仅供本地测试。

## 3. GitHub-first 发布门禁

### 3.1 本地验证

发布内容必须先形成 GitHub commit；不得先修改线上文件再反向补仓库。

在干净工作树执行：

```powershell
git status --short --branch
python -m py_compile ops\dramawave-attribution-comparison\common.py ops\dramawave-attribution-comparison\refresh_cache.py ops\dramawave-attribution-comparison\service.py
python -m unittest -v ops\dramawave-attribution-comparison\test_backend.py ops\dramawave-attribution-comparison\test_frontend_contract.py
git diff --check
```

确认只包含本需求文件后，提交并推送 `codex/...` 分支，记录不可变 commit SHA。发布审批使用 SHA，不使用会移动的分支名。

```powershell
git add ops/dramawave-attribution-comparison doc/003.dramawave-attribution-comparison-dashboard
git commit -m "Add Dramawave attribution comparison dashboard"
git push -u origin <branch>
git rev-parse HEAD
```

发布前把目标 SHA 填入“发布状态回填”，并确认 GitHub 远端存在该对象和对应 PR/审核记录。

### 3.2 服务器从精确 commit 取文件

以下变量必须替换为已审核值，不得直接复制占位符执行：

```bash
TARGET_SHA='<待发布回填的40位commit>'
SOURCE_REPO='/root/drama_material_service'
RELEASE_ROOT='/opt/dramawave-attribution-comparison/releases'
RELEASE_PATH="${RELEASE_ROOT}/${TARGET_SHA}"
```

从 GitHub 更新对象并确认 SHA：

```bash
git -C "$SOURCE_REPO" fetch --all --prune
git -C "$SOURCE_REPO" cat-file -e "${TARGET_SHA}^{commit}"
git -C "$SOURCE_REPO" show -s --format='%H %ci %s' "$TARGET_SHA"
```

通过 `git archive` 从该 SHA 构建独立 release，不同步或覆盖主 AI 后台目录：

```bash
install -d -m 0755 "$RELEASE_ROOT"
test ! -e "$RELEASE_PATH"
install -d -m 0755 "$RELEASE_PATH"
git -C "$SOURCE_REPO" archive "$TARGET_SHA" ops/dramawave-attribution-comparison \
  | tar -x -C "$RELEASE_PATH" --strip-components=2
printf '%s\n' "$TARGET_SHA" > "$RELEASE_PATH/SOURCE_COMMIT"
chmod -R go-w "$RELEASE_PATH"
```

在切换前使用固定虚拟环境验证 release。虚拟环境的创建步骤见第 5 节；不得回退到系统 Python 或未固定版本的全局包：

```bash
cd "$RELEASE_PATH"
VENV_PY=/opt/dramawave-attribution-comparison/venv/bin/python
"$VENV_PY" -m py_compile common.py refresh_cache.py service.py
"$VENV_PY" -m unittest -v test_backend.py test_frontend_contract.py
"$VENV_PY" refresh_cache.py --help
"$VENV_PY" service.py --help
systemd-analyze verify deploy/dramawave-attribution-comparison.service \
  deploy/dramawave-attribution-comparison-refresh.service \
  deploy/dramawave-attribution-comparison-refresh.timer
```

## 4. 发布前只读核验与备份

### 4.1 数据盘和端口

```bash
findmnt -n -o TARGET,SOURCE,FSTYPE,UUID -T /mnt/data-disk
test "$(findmnt -n -o UUID -T /mnt/data-disk)" = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
test -w /mnt/data-disk
df -h /mnt/data-disk
ss -ltnp | grep ':8832 ' || true
```

如果 UUID 不匹配、数据盘不可写、空间不足或 8832 已被其他进程占用，停止发布。

### 4.2 记录发布前状态

```bash
readlink -f /opt/dramawave-attribution-comparison/current || true
systemctl is-enabled dramawave-attribution-comparison.service 2>/dev/null || true
systemctl is-active dramawave-attribution-comparison.service 2>/dev/null || true
systemctl is-enabled dramawave-attribution-comparison-refresh.timer 2>/dev/null || true
systemctl is-active dramawave-attribution-comparison-refresh.timer 2>/dev/null || true
systemctl list-timers dramawave-attribution-comparison-refresh.timer --all --no-pager || true
nginx -T 2>/dev/null | grep -n 'dramawave-attribution-comparison' || true
```

把输出保存到发布前备份目录。建议路径模式：

```text
/mnt/data-disk/dramawave-attribution-comparison/backups/<YYYYMMDD-HHMMSS>-pre-<shortsha>
```

实际创建后必须将完整路径回填到本文顶部，不能把建议路径写成已完成证据。

按目标 SHA 创建备份目录并保存当前状态：

```bash
BACKUP_TS="$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S)"
SHORT_SHA="$(printf '%s' "$TARGET_SHA" | cut -c1-12)"
BACKUP_DIR="/mnt/data-disk/dramawave-attribution-comparison/backups/${BACKUP_TS}-pre-${SHORT_SHA}"
install -d -o root -g root -m 0700 "$BACKUP_DIR"

readlink -f /opt/dramawave-attribution-comparison/current > "$BACKUP_DIR/previous-current.txt" 2>&1 || true
systemctl is-enabled dramawave-attribution-comparison.service > "$BACKUP_DIR/web-enabled.txt" 2>&1 || true
systemctl is-active dramawave-attribution-comparison.service > "$BACKUP_DIR/web-active.txt" 2>&1 || true
systemctl is-enabled dramawave-attribution-comparison-refresh.timer > "$BACKUP_DIR/timer-enabled.txt" 2>&1 || true
systemctl is-active dramawave-attribution-comparison-refresh.timer > "$BACKUP_DIR/timer-active.txt" 2>&1 || true
systemctl list-timers dramawave-attribution-comparison-refresh.timer --all --no-pager > "$BACKUP_DIR/timers.txt" 2>&1 || true
ss -ltnp > "$BACKUP_DIR/listeners.txt"
nginx -T > "$BACKUP_DIR/nginx-expanded.conf" 2>&1

for unit in \
  dramawave-attribution-comparison.service \
  dramawave-attribution-comparison-refresh.service \
  dramawave-attribution-comparison-refresh.timer
do
  if test -f "/etc/systemd/system/$unit"; then
    install -o root -g root -m 0644 "/etc/systemd/system/$unit" "$BACKUP_DIR/$unit"
  fi
done

if test -f /etc/nginx/default.d/dramawave-attribution-comparison.conf; then
  install -o root -g root -m 0644 \
    /etc/nginx/default.d/dramawave-attribution-comparison.conf \
    "$BACKUP_DIR/dramawave-attribution-comparison.conf"
fi
if test -f /mnt/data-disk/dramawave-attribution-comparison/dashboard.env; then
  install -o root -g root -m 0600 \
    /mnt/data-disk/dramawave-attribution-comparison/dashboard.env \
    "$BACKUP_DIR/dashboard.env"
fi
```

### 4.3 创建可验证备份

备份至少包含：

- `current` 旧指向和旧 `SOURCE_COMMIT`。
- 旧 systemd units、Nginx 配置。
- `/mnt/data-disk/dramawave-attribution-comparison/dashboard.env`，若已存在。
- SQLite 在线备份，若数据库已存在。
- unit/timer 状态、端口、Nginx 展开配置和文件 SHA-256 清单。

SQLite 开启 WAL，运行中不得只复制主 `.sqlite3` 文件。使用 SQLite online backup：

```bash
DB=/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3
if test -f "$DB"; then
  sqlite3 "$DB" ".backup '$BACKUP_DIR/dashboard.sqlite3'"
  sqlite3 "$BACKUP_DIR/dashboard.sqlite3" 'PRAGMA quick_check;'
fi
```

D10 切换前，此处的 `dashboard.sqlite3` 是历史 D30 回滚库。备份完成后记录其 `PRAGMA quick_check`、`data_version`、文件大小和 SHA-256；不得重命名、覆盖或用 D10 schema 改写它。D10 候选库使用独立路径，bootstrap 完成且无写进程时另行执行 `PRAGMA wal_checkpoint(TRUNCATE)`、`PRAGMA quick_check` 和 SHA-256 记录。

环境文件备份必须保持 root-only 权限，不打印内容：

```bash
chmod 0700 "$BACKUP_DIR"
chmod 0600 "$BACKUP_DIR/dashboard.env" 2>/dev/null || true
```

为备份目录内的文件生成相对路径 SHA-256 清单，并在备份目录中运行 `sha256sum -c`。清单不能指回仍会变化的线上源文件。

```bash
cd "$BACKUP_DIR"
find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
sha256sum -c SHA256SUMS
```

## 5. 环境和目录准备

创建数据目录：

```bash
install -d -o root -g root -m 0700 /mnt/data-disk/dramawave-attribution-comparison
install -d -o root -g root -m 0700 /mnt/data-disk/dramawave-attribution-comparison/cache
install -d -o root -g root -m 0700 /mnt/data-disk/dramawave-attribution-comparison/backups
```

创建服务专用 venv，并从 release 内的精确版本依赖清单安装。首次部署必须执行；后续 release 仅在 `requirements.txt` 变化时重建并复验：

```bash
install -d -o root -g root -m 0755 /opt/dramawave-attribution-comparison
python3 -m venv /opt/dramawave-attribution-comparison/venv
/opt/dramawave-attribution-comparison/venv/bin/python -m pip install --disable-pip-version-check \
  -r "$RELEASE_PATH/requirements.txt"
/opt/dramawave-attribution-comparison/venv/bin/python -c \
  'import pymysql; print("PyMySQL import ok")'
```

当前历史 D30 生产的 `dashboard.env` 至少包含：

```dotenv
DRAMAWAVE_ATTRIBUTION_DB_PATH=/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3
```

在 D10 候选验证完成前保持该文件不变。候选发布时先创建同目录临时文件，指向已经核验的独立 D10 库；不要提前覆盖活动环境文件：

```bash
D10_CANDIDATE_DB="/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard-d10-${SHORT_SHA}.sqlite3"
D10_ENV_NEXT=/mnt/data-disk/dramawave-attribution-comparison/dashboard.env.d10.next
test -f "$D10_CANDIDATE_DB"
printf 'DRAMAWAVE_ATTRIBUTION_DB_PATH=%s\n' "$D10_CANDIDATE_DB" > "$D10_ENV_NEXT"
chown root:root "$D10_ENV_NEXT"
chmod 0600 "$D10_ENV_NEXT"
```

如果现有 `dashboard.env` 还含其他非 MySQL 配置，必须逐项安全复制到 `.d10.next`；不得输出文件内容或丢失既有键。第 8 节仅在所有 D10 门禁通过后用 `mv -Tf` 原子替换活动环境文件。

```bash
chown root:root /mnt/data-disk/dramawave-attribution-comparison/dashboard.env
chmod 0600 /mnt/data-disk/dramawave-attribution-comparison/dashboard.env
```

不要在 `dashboard.env` 中复制 MySQL 密码。refresh unit 已单独读取现有 `/root/drama_material_service/.env`；该文件必须提供：

```text
ADMIN_MAPPING_MYSQL_HOST
ADMIN_MAPPING_MYSQL_PORT=63350
ADMIN_MAPPING_MYSQL_USER
ADMIN_MAPPING_MYSQL_PASSWORD
ADMIN_MAPPING_MYSQL_DATABASE
```

只核对键名和端口，不输出密钥值。Web unit 不读取该文件。

## 6. 安装 release 和部署配置

以下 `current` 切换是首次部署/普通同语义发布的通用步骤。D10 迁移不得在候选库完成前执行；先直接从 `$RELEASE_PATH` 运行第 7 节 bootstrap 和验证，再在第 8 节停服窗口内与数据库指针一起切换。

普通同语义发布原子切换 `current`：

```bash
ln -s "$RELEASE_PATH" /opt/dramawave-attribution-comparison/current.next
mv -Tf /opt/dramawave-attribution-comparison/current.next /opt/dramawave-attribution-comparison/current
test "$(cat /opt/dramawave-attribution-comparison/current/SOURCE_COMMIT)" = "$TARGET_SHA"
```

安装 units 和 Nginx 配置。该 Nginx 文件只包含 `location`，必须放在当前主机明确位于 HTTPS/HTTP `server {}` 内的 `/etc/nginx/default.d/`；不得放入 `http {}` 层的 `/etc/nginx/conf.d/`：

```bash
install -o root -g root -m 0644 \
  "$RELEASE_PATH/deploy/dramawave-attribution-comparison.service" \
  /etc/systemd/system/dramawave-attribution-comparison.service
install -o root -g root -m 0644 \
  "$RELEASE_PATH/deploy/dramawave-attribution-comparison-refresh.service" \
  /etc/systemd/system/dramawave-attribution-comparison-refresh.service
install -o root -g root -m 0644 \
  "$RELEASE_PATH/deploy/dramawave-attribution-comparison-refresh.timer" \
  /etc/systemd/system/dramawave-attribution-comparison-refresh.timer
install -o root -g root -m 0644 \
  "$RELEASE_PATH/deploy/dramawave-attribution-comparison.nginx.conf" \
  /etc/nginx/default.d/dramawave-attribution-comparison.conf

systemctl daemon-reload
nginx -t
```

首次部署此时不要先启 Web 或 timer。D10 迁移在完成独立候选库前还不得安装会被生产 service 使用的新 units 或切换 `current`；`service.py` 在缓存不存在、结构不符或语义标记不匹配时均应退出码 2。

## 7. 首次 bootstrap

### 7.1 D10 日期范围与候选路径

用户已批准起点为 `2026-08-01`。部署时 `BOOTSTRAP_START` 必须固定为该值，并与目标 commit 的 `MIN_DATE` 完全相同：

```bash
BOOTSTRAP_START='2026-08-01'
BOOTSTRAP_END="$(TZ=Asia/Shanghai date +%F)"
D10_CANDIDATE_DB="/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard-d10-${SHORT_SHA}.sqlite3"
test "$BOOTSTRAP_START" = '2026-08-01'
EXPECTED_MIN_DATE="$($VENV_PY - <<'PY'
from common import MIN_DATE
print(MIN_DATE.isoformat())
PY
)"
test "$BOOTSTRAP_START" = "$EXPECTED_MIN_DATE"
test ! -e "$D10_CANDIDATE_DB"
```

脚本拒绝未来日期，并自动删除/忽略 60 天保留窗口之外的日期。实际起点为 `max(MIN_DATE, 北京当天-59天)`；传入的 `BOOTSTRAP_START` 不能绕过代码边界。若 D10 源侧在批准范围内存在任一日期空洞，停止发布，不得用历史 D30 行或 0 值补洞。

### 7.2 使用临时 systemd oneshot 执行

不要在交互 shell 中打印或手工拼接 MySQL 密钥。使用两个现有 `EnvironmentFile` 运行一次性 bootstrap：

```bash
BOOTSTRAP_UNIT="dramawave-attribution-comparison-bootstrap-$(date +%Y%m%d%H%M%S)"
systemd-run --unit="$BOOTSTRAP_UNIT" --wait --collect --pipe \
  --property=Type=oneshot \
  --property=WorkingDirectory="$RELEASE_PATH" \
  --property=EnvironmentFile=/mnt/data-disk/dramawave-attribution-comparison/dashboard.env \
  --property=EnvironmentFile=/root/drama_material_service/.env \
  --property=RequiresMountsFor=/mnt/data-disk \
  --property=TimeoutStartSec=45min \
  --property=MemoryAccounting=true \
  --property=MemoryHigh=800M \
  --property=MemoryMax=1G \
  --property=NoNewPrivileges=true \
  --property=PrivateTmp=false \
  --property=ProtectSystem=strict \
  --property=ProtectHome=read-only \
  --property=ReadOnlyPaths="$RELEASE_PATH" \
  --property=ReadOnlyPaths=/root/drama_material_service/.env \
  --property=ReadWritePaths=/tmp \
  --property=ReadWritePaths=/mnt/data-disk/dramawave-attribution-comparison \
  /usr/bin/flock -E 75 -xn /tmp/tt_minis_multi_dim_dashboard.lock \
  /opt/dramawave-attribution-comparison/venv/bin/python \
  "$RELEASE_PATH/refresh_cache.py" \
  --db-path "$D10_CANDIDATE_DB" \
  --bootstrap-start "$BOOTSTRAP_START" --bootstrap-end "$BOOTSTRAP_END"
```

执行前必须确认现有 TT 进程已经退出且共享锁空闲；非阻塞 flock 在锁被占用时以专用退出码 `75` 拒绝启动，不得移除锁后重试。bootstrap 受与定时 refresh 相同的 1GB cgroup 硬上限保护，并显式保持主机 `/tmp` 可见、可创建锁文件，以确保共享锁真实生效。bootstrap 只有全部目标日期完成 staging 后，才会在一个 `BEGIN IMMEDIATE` 事务中替换事实数据并推进 `data_version`。任一天源查询或映射校验失败都会清除本轮 staging、把已创建日志标成 failed，并保留旧事实和旧版本。

成功后核验：

```bash
DB="$D10_CANDIDATE_DB"
sqlite3 "$DB" 'PRAGMA quick_check;'
sqlite3 "$DB" "SELECT MIN(dt),MAX(dt),COUNT(*) FROM attribution_fact;"
sqlite3 "$DB" "SELECT 'fact',COUNT(*) FROM attribution_fact UNION ALL SELECT 'filter_daily',COUNT(*) FROM attribution_filter_daily UNION ALL SELECT 'campaign_daily',COUNT(*) FROM attribution_campaign_daily;"
sqlite3 "$DB" "SELECT key,value FROM cache_meta WHERE key IN ('comparison_window','new_attribution_source','data_version','rollup_version','generated_at','last_refresh_dates','source_max_updated_at') ORDER BY key;"
sqlite3 "$DB" "SELECT dt,status,fact_rows,data_version FROM refresh_log ORDER BY id DESC LIMIT 20;"
test "$(sqlite3 "$DB" 'SELECT MIN(dt) FROM attribution_fact;')" = '2026-08-01'
test "$(sqlite3 "$DB" "SELECT COUNT(*) FROM attribution_fact WHERE dt<'2026-08-01';")" = '0'
test "$(sqlite3 "$DB" "SELECT COUNT(*) FROM attribution_filter_daily WHERE dt<'2026-08-01';")" = '0'
test "$(sqlite3 "$DB" "SELECT COUNT(*) FROM attribution_campaign_daily WHERE dt<'2026-08-01';")" = '0'
test "$(sqlite3 "$DB" "SELECT COUNT(*) FROM refresh_log WHERE dt<'2026-08-01';")" = '0'
sqlite3 "$DB" 'PRAGMA wal_checkpoint(TRUNCATE);'
```

要求：`quick_check=ok`、实际起点为 `2026-08-01`、三层和刷新日志均无更早数据、三层均有数据、所有 bootstrap 日期日志为 success、`data_version` 非空且与 `rollup_version` 完全相等，且标记精确为 `comparison_window="D10"`、`new_attribution_source="kunlunads_dev.ads_app_revenues_10d"`。候选 HTTP 验收还必须确认 `minimum_date=expected_start_date=2026-08-01`、`range_complete=true`、`missing_dates=[]`。任何不符都删除候选发布资格，保留历史 D30 生产不动；不得修改标记来掩盖错误数据。

再验证 D10 release 会拒绝历史 D30 数据库。两个命令都必须非零退出，日志必须包含 D10 cache contract rejection；执行期间 timer/Web 仍运行历史 D30 release，D10 检查仅以只读方式打开旧库：

```bash
D30_DB=/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3
set +e
DRAMAWAVE_ATTRIBUTION_DB_PATH="$D30_DB" "$VENV_PY" "$RELEASE_PATH/service.py" \
  --host 127.0.0.1 --port 0 > "$BACKUP_DIR/d10-web-rejects-d30.log" 2>&1
WEB_REJECT_RC=$?
/usr/bin/flock -E 75 -xn /tmp/tt_minis_multi_dim_dashboard.lock \
  "$VENV_PY" "$RELEASE_PATH/refresh_cache.py" --db-path "$D30_DB" --date "$BOOTSTRAP_START" \
  > "$BACKUP_DIR/d10-refresh-rejects-d30.log" 2>&1
REFRESH_REJECT_RC=$?
set -e
test "$WEB_REJECT_RC" -eq 2
test "$REFRESH_REJECT_RC" -eq 1
grep -q 'D10 cache contract rejected' "$BACKUP_DIR/d10-web-rejects-d30.log"
grep -q 'D10 cache contract rejected' "$BACKUP_DIR/d10-refresh-rejects-d30.log"
```

## 8. 启动 Web、Nginx 和 timer

### 8.1 D10 停服窗口内原子提升

先完成第 7 节全部验证并创建 `dashboard.env.d10.next`。记录历史 D30 release/数据库/环境文件为回滚点，然后停止所有可能访问活动数据库的进程：

```bash
D30_RELEASE="$(readlink -f /opt/dramawave-attribution-comparison/current)"
D30_DB=/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3
D10_CANDIDATE_DB="/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard-d10-${SHORT_SHA}.sqlite3"
D10_ENV_NEXT=/mnt/data-disk/dramawave-attribution-comparison/dashboard.env.d10.next
test -d "$D30_RELEASE"
test -f "$D30_DB"
test -f "$D10_CANDIDATE_DB"
test -f "$D10_ENV_NEXT"

systemctl stop dramawave-attribution-comparison-refresh.timer
systemctl stop dramawave-attribution-comparison-refresh.service 2>/dev/null || true
systemctl stop dramawave-attribution-comparison.service
test -z "$(lsof -t "$D30_DB" "$D10_CANDIDATE_DB" 2>/dev/null || true)"
```

在服务停止期间安装已审核 D10 units/Nginx 配置，准备新 release 软链接。`nginx -t` 不通过时不要切换：

```bash
install -o root -g root -m 0644 "$RELEASE_PATH/deploy/dramawave-attribution-comparison.service" /etc/systemd/system/
install -o root -g root -m 0644 "$RELEASE_PATH/deploy/dramawave-attribution-comparison-refresh.service" /etc/systemd/system/
install -o root -g root -m 0644 "$RELEASE_PATH/deploy/dramawave-attribution-comparison-refresh.timer" /etc/systemd/system/
install -o root -g root -m 0644 "$RELEASE_PATH/deploy/dramawave-attribution-comparison.nginx.conf" /etc/nginx/default.d/dramawave-attribution-comparison.conf
systemctl daemon-reload
nginx -t

ln -s "$RELEASE_PATH" /opt/dramawave-attribution-comparison/current.d10.next
test "$(cat "$RELEASE_PATH/SOURCE_COMMIT")" = "$TARGET_SHA"
```

活动 DB 不是通过复制或覆盖 `.sqlite3` 替换，而是通过同文件系统的环境文件 rename 原子替换指针；`current` 也使用原子 rename。两次 rename 期间 Web/refresh 均停止，因此不存在用户可见的代码/数据库混合态：

```bash
mv -Tf /opt/dramawave-attribution-comparison/current.d10.next /opt/dramawave-attribution-comparison/current
mv -Tf "$D10_ENV_NEXT" /mnt/data-disk/dramawave-attribution-comparison/dashboard.env
test "$(grep -cF "DRAMAWAVE_ATTRIBUTION_DB_PATH=$D10_CANDIDATE_DB" /mnt/data-disk/dramawave-attribution-comparison/dashboard.env)" -eq 1
```

`mv -Tf` 之后不得再改 D10 SQLite 的语义标记。若 D10 code、环境指针或候选库任一不匹配，Web 启动会失败关闭；直接执行第 10.0 节回滚，不得临时把环境指回 D30 让 D10 code 强行启动。

### 8.2 启动并分阶段开放

```bash
systemctl enable dramawave-attribution-comparison.service
systemctl start dramawave-attribution-comparison.service
systemctl status dramawave-attribution-comparison.service --no-pager
journalctl -u dramawave-attribution-comparison.service -n 100 --no-pager

curl -fsS http://127.0.0.1:8832/healthz
curl -fsS http://127.0.0.1:8832/api/meta
/opt/dramawave-attribution-comparison/venv/bin/python \
  /opt/dramawave-attribution-comparison/current/warm_cache.py \
  --attempts 3 --retry-delay 1
```

Web unit 会在每次进程启动后带短重试预热默认聚合；refresh oneshot 会在每个新 `data_version` 提交后再次预热。两处均使用 `ExecStartPost=-...`，预热失败会写日志但不会把结构健康的 Web/刷新误判为失败。首次发布仍必须在 health 通过后显式执行上面的命令并记录 JSON 输出，避免 bootstrap 时 Web 尚未启动而留下冷缓存。

确认 loopback 正常后再使 Nginx 配置生效。先执行 `nginx -t`；如果该主机的 Nginx 由 systemd 管理，执行 `systemctl reload nginx`，否则使用主机现有的安全 reload/HUP 方式，不要因 reload 单元不存在而重启整台服务。

只有第 9 节 D10 API、语义标记、Nginx/飞书和真实点样本均通过后，才启用 timer：

```bash
systemctl enable --now dramawave-attribution-comparison-refresh.timer
systemctl status dramawave-attribution-comparison-refresh.timer --no-pager
systemctl list-timers dramawave-attribution-comparison-refresh.timer --all --no-pager
```

timer 在每小时 `:22` 和 `:52` 触发，允许最多 5 秒随机延迟；`Persistent=true` 会补跑错过的计划。该错峰仍严格保持 30 分钟频率。refresh 还与现有 TT 多维看板共用 `/tmp/tt_minis_multi_dim_dashboard.lock`：任一重任务正在运行时另一方不得并发，避免 3.67GiB 共享主机进入换页风暴。flock 的专用退出码 `75` 仅表示本轮因锁忙正常跳过；Python 的退出码 `1` 仍是 refresh 失败，不能被吞掉。refresh cgroup 同时设置 `MemoryHigh=800M`、`MemoryMax=1G`；触顶时保留上一成功版本，不允许拖垮既有服务。普通运行不带参数，刷新北京今天、昨天，并按 `history_cursor` 轮转一个更早日期。

目标机是 systemd 239 + cgroup v1；发布前瞬态单元实测 `MemoryMax=1G` 写入 `memory.limit_in_bytes=1073741824`，因此 1GB 是有效硬限制。`MemoryHigh` 在该主机仅保留为迁移到 cgroup v2 后的软门槛，当前不能把它当作已生效的 800MB 限制。

## 9. 发布验证

### 9.1 进程和权限边界

```bash
ss -ltnp | grep '127.0.0.1:8832'
systemctl show dramawave-attribution-comparison.service -p MainPID -p FragmentPath -p WorkingDirectory
systemctl show dramawave-attribution-comparison-refresh.service -p FragmentPath -p WorkingDirectory
```

仅检查 Web 进程环境变量名，不打印值，确认不存在 `ADMIN_MAPPING_MYSQL_`：

```bash
WEB_PID="$(systemctl show -p MainPID --value dramawave-attribution-comparison.service)"
tr '\0' '\n' < "/proc/${WEB_PID}/environ" | cut -d= -f1 | grep '^ADMIN_MAPPING_MYSQL_' && exit 1 || true
```

### 9.2 API 与缓存版本

```bash
curl -fsS -D /tmp/dramawave-attr-health.headers -o /tmp/dramawave-attr-health.json \
  http://127.0.0.1:8832/healthz
curl -fsS -D /tmp/dramawave-attr-meta.headers -o /tmp/dramawave-attr-meta.json \
  http://127.0.0.1:8832/api/meta
sqlite3 "$D10_CANDIDATE_DB" \
  "SELECT key,value FROM cache_meta WHERE key IN ('comparison_window','new_attribution_source','data_version','rollup_version') ORDER BY key;"
```

验证 `ok=true`、`stale=false`、缓存起点等于批准边界经 60 天裁剪后的日期、终点为北京当天，并检查 `Cache-Control`、`ETag`。`/api/meta.source_tables.new_attribution` 必须是 `kunlunads_dev.ads_app_revenues_10d`，API/CSV 只能出现 `d7_*` / `d10_*`，不得残留 `d30_*`。SQLite 标记必须是 `comparison_window="D10"`、`new_attribution_source="kunlunads_dev.ads_app_revenues_10d"` 且 `data_version=rollup_version`。使用 meta 返回的日期和 `data_version` 调用 `/api/options`、`/api/query?include_rankings=0`、`/api/rankings`、完整兼容 `/api/query` 和 `/api/export.csv`；把旧版本传给 query/rankings，确认返回 409。

在批准范围内重新选取一个同时存在 custom、D7 和 D10 数据的真实 Ad 点样本，以 63350 只读查询分别对账；不得复用 D30 的 `2026-07-29 / ad_id=120250136876120737` 数值作为 D10 证据。点样本和全量汇总守恒未通过时，立即按第 10.0 节回滚。

对当前真实全范围和 7/30/60 天可用范围分别执行默认、单渠道、单投放产品、单国家组、单优化师及 Ad Set 分组。每个新版本提交后必须先完成默认 7 天/全范围、D0/D7、常用分组和排行预热，再开放用户请求；用户可见常用路径目标不高于 300 毫秒。未预热的低基数组合目标不高于 1 秒，Campaign/Ad Set 宽范围尾部查询应异步或纳入下一版预热，不得阻塞默认首屏。若真实历史尚不足 30/60 天，记录当前全范围结果并用同量级合成 SQLite 验证容量，不把尚不存在的历史伪装成真实 30/60 天证据。

### 9.3 Nginx / 飞书鉴权

```bash
nginx -t
curl -sSI https://ai.yingliangads.com/reports/dramawave-attribution-comparison/
curl -sSI https://ai.yingliangads.com/reports/dramawave-attribution-comparison/api/meta
```

未登录访问应进入飞书登录流程，不能直接泄露页面或 API。再用已授权飞书会话在浏览器验证页面、筛选、分页、CSV、控制台和网络请求。

### 9.4 自然 timer 证据

不要用手工 `systemctl start ...refresh.service` 代替自然调度证据。等下一次 `:22` 或 `:52` 自然触发后检查：

```bash
systemctl list-timers dramawave-attribution-comparison-refresh.timer --all --no-pager
journalctl -u dramawave-attribution-comparison-refresh.service --since '-45 minutes' --no-pager
sqlite3 "$D10_CANDIDATE_DB" \
  "SELECT key,value FROM cache_meta WHERE key IN ('data_version','rollup_version','generated_at','last_refresh_dates','history_cursor') ORDER BY key;"
sqlite3 "$D10_CANDIDATE_DB" \
  "SELECT dt,status,fact_rows,data_version FROM refresh_log ORDER BY id DESC LIMIT 6;"
```

要求本轮为 success、`last_refresh_dates` 含今天/昨天/一个历史日期、版本已推进、Web health 显示新版本。

2026-08-06 历史 D30 第一轮自然 timer 证据：`21:22:00` 启动，依次刷新 `2026-08-06`、`2026-08-05`、`2026-07-29`，facts 分别为 `99,000`、`131,011`、`75,283`；`21:26:12` 提交版本 `20260806T132547Z-d44a154d`，`21:27:01` 完成预热。该记录不能代替 D10 自然 timer 验收；D10 切换后必须另行回填新证据。

### 9.5 2026-08-06 历史 D30 生产实测记录

以下数值均属于 D30 历史基线，不是 D10 生产实测；D10 切换后必须另建小节回填真实版本、范围、行数、性能、资源和回滚点。

- 单日最大量 canary（`2026-08-05`）：`131,010` facts，约 93 秒，观测峰值 `809,607,168` bytes，低于 900 MiB 门禁；SQLite `quick_check=ok`，汇总最大误差 `<5e-7`。
- 全量 bootstrap：实际源读取/发布 `20:51:17`～`21:02:29`，共 `920,751` facts；数据库约 `1.34 GiB`，WAL 已归零；1 GiB cgroup 硬限制生效，`oom_kill=0`。
- 第一轮自然刷新后：`920,753` facts、`70,729` filter rollups、`898,655` campaign rollups；三层汇总最大误差 `<4.74e-7`。
- 新版本预热后，全范围 Campaign、Ad Set、排行接口分别约 `2.295 ms`、`1.787 ms`、`1.543 ms`；未预热的 optimizer×country 组合约 `248 ms`。gzip、ETag/304、`Cache-Control: private` 均通过。
- Web 常驻 RSS `35,580 KiB`，`VmHWM 177,524 KiB`，`VmSwap 0`。Web service 与 refresh timer 均为 active/enabled，Nginx 配置检查无 warning。
- 公网未登录页面/API 均 `302` 进入飞书登录。当前执行环境没有可复用的已授权飞书浏览器会话，因此已授权后的生产页面视觉检查由首次登录用户补验；本地真实浏览器 fixture 已覆盖桌面/移动和全部关键交互。
- timer 每 `:22/:52` 发起一次刷新并与 TT 刷新共用宿主锁。锁忙以专用状态 `75` 安全跳过且保留旧版本；这保证不并发挤压生产源库，但在 TT 单轮超过 30 分钟时，不能承诺每个调度点都完成计算。严格的“每 30 分钟必完成”SLO 需要独立资源窗口或另行调整 TT 调度。

## 10. 精确回滚

### 10.0 D10 切换回滚到历史 D30（本次迁移默认）

D10 发布失败时必须同时切回历史 D30 release 和历史 D30 数据库指针；D10 code 会主动拒绝 D30 SQLite，因此不能只回滚其中一个。候选 D10 数据库原样保留作审计，不复制、不覆盖、不删除。

```bash
systemctl stop dramawave-attribution-comparison-refresh.timer
systemctl stop dramawave-attribution-comparison-refresh.service 2>/dev/null || true
systemctl stop dramawave-attribution-comparison.service

D30_RELEASE='<从发布前 previous-current.txt 回填>'
D30_DB=/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3
D10_CANDIDATE_DB='<从 D10 发布记录回填候选库绝对路径>'
BACKUP_DIR='<从发布状态记录回填>'
test -d "$D30_RELEASE"
test -f "$D30_DB"
test -f "$BACKUP_DIR/dashboard.env"
test -z "$(lsof -t "$D30_DB" "$D10_CANDIDATE_DB" 2>/dev/null || true)"
sqlite3 "$D30_DB" 'PRAGMA quick_check;'

ln -s "$D30_RELEASE" /opt/dramawave-attribution-comparison/current.d30.rollback.next
install -o root -g root -m 0600 "$BACKUP_DIR/dashboard.env" \
  /mnt/data-disk/dramawave-attribution-comparison/dashboard.env.d30.rollback.next

mv -Tf /opt/dramawave-attribution-comparison/current.d30.rollback.next \
  /opt/dramawave-attribution-comparison/current
mv -Tf /mnt/data-disk/dramawave-attribution-comparison/dashboard.env.d30.rollback.next \
  /mnt/data-disk/dramawave-attribution-comparison/dashboard.env

install -o root -g root -m 0644 "$BACKUP_DIR/dramawave-attribution-comparison.service" /etc/systemd/system/
install -o root -g root -m 0644 "$BACKUP_DIR/dramawave-attribution-comparison-refresh.service" /etc/systemd/system/
install -o root -g root -m 0644 "$BACKUP_DIR/dramawave-attribution-comparison-refresh.timer" /etc/systemd/system/
install -o root -g root -m 0644 "$BACKUP_DIR/dramawave-attribution-comparison.conf" /etc/nginx/default.d/
systemctl daemon-reload
nginx -t
if systemctl cat nginx.service >/dev/null 2>&1; then
  systemctl reload nginx
else
  nginx -s reload
fi
systemctl start dramawave-attribution-comparison.service

# D30 库在 D10 在线期间保持冻结；先执行一次旧 release 的正常刷新。
# oneshot 自带共享 TT 锁、1 GiB 限制和旧 warm_cache ExecStartPost。
systemctl start dramawave-attribution-comparison-refresh.service
curl -fsS http://127.0.0.1:8832/healthz
curl -fsS http://127.0.0.1:8832/api/meta

# 再显式预热，避免刚才共享锁返回 75 或 Web 启动时序使 ExecStartPost 未命中。
/opt/dramawave-attribution-comparison/venv/bin/python \
  "$D30_RELEASE/warm_cache.py" --base-url http://127.0.0.1:8832 \
  --attempts 3 --retry-delay 1

# 严格按发布前记录恢复 timer 的 enabled / active 两个独立状态。
if grep -qx enabled "$BACKUP_DIR/timer-enabled.txt"; then
  systemctl enable dramawave-attribution-comparison-refresh.timer
else
  systemctl disable dramawave-attribution-comparison-refresh.timer
fi
if grep -qx active "$BACKUP_DIR/timer-active.txt"; then
  systemctl start dramawave-attribution-comparison-refresh.timer
else
  systemctl stop dramawave-attribution-comparison-refresh.timer
fi
```

确认 `current`、活动 DB 路径和 `/api/meta.source_tables.new_attribution` 已回到 `kunlunads_dev.ads_app_revenues_30d`。D30 数据库在 D10 在线期间没有刷新，因此不能只核对切换前旧 `data_version`：还必须确认 health 为 200、`cache.stale=false`、`cache.range_complete=true`、`cache.end_date` 为北京时间今天且无 `missing_dates`。若切换已超过两天或默认刷新后仍有缺日，保持 D30 Web 可读但 timer 暂不恢复，使用第 7.2 节相同的 hardened transient oneshot、旧 `D30_RELEASE` 和 `D30_DB`，从该库当前 `MIN(dt)` 到今天执行一次完整 bootstrap 补刷；通过上述门禁并重新运行旧 `warm_cache.py` 后，再按备份状态恢复 timer。不得因为 D10 候选仍在磁盘上而让旧 timer 指向它。

### 10.1 代码/配置回滚（默认）

本节仅适用于归因语义和 SQLite 合同未变化的普通代码发布，不适用于 D30→D10 迁移。D10 迁移必须使用第 10.0 节，同时切回旧 release 与旧数据库指针。普通发布默认回滚不删除或回退 SQLite，先保留新数据用于审计。执行顺序：

1. 停止 timer，防止回滚过程中刷新写入。
2. 记录当前 `current`、版本、health 和最新日志。
3. 将 `current` 原子切回发布前记录的 release。
4. 从发布前备份恢复三份 unit 和 Nginx 配置。
5. `daemon-reload`、`nginx -t`，重启 Web；确认无误后按旧状态决定是否恢复旧 timer。

```bash
systemctl stop dramawave-attribution-comparison-refresh.timer
systemctl stop dramawave-attribution-comparison-refresh.service 2>/dev/null || true

PREVIOUS_RELEASE='<从发布前记录回填>'
BACKUP_DIR='<从发布状态回填表取得>'
test -d "$PREVIOUS_RELEASE"
test -d "$BACKUP_DIR"

ln -s "$PREVIOUS_RELEASE" /opt/dramawave-attribution-comparison/current.rollback
mv -Tf /opt/dramawave-attribution-comparison/current.rollback /opt/dramawave-attribution-comparison/current

install -o root -g root -m 0644 "$BACKUP_DIR/dramawave-attribution-comparison.service" /etc/systemd/system/
install -o root -g root -m 0644 "$BACKUP_DIR/dramawave-attribution-comparison-refresh.service" /etc/systemd/system/
install -o root -g root -m 0644 "$BACKUP_DIR/dramawave-attribution-comparison-refresh.timer" /etc/systemd/system/
install -o root -g root -m 0644 "$BACKUP_DIR/dramawave-attribution-comparison.conf" /etc/nginx/default.d/

systemctl daemon-reload
nginx -t
systemctl reload nginx
systemctl restart dramawave-attribution-comparison.service
curl -fsS http://127.0.0.1:8832/healthz
/opt/dramawave-attribution-comparison/venv/bin/python \
  /opt/dramawave-attribution-comparison/current/warm_cache.py --attempts 3 --retry-delay 1
```

按发布前记录恢复 Nginx 配置并在 `nginx -t` 通过后执行安全 reload，再恢复 timer 的 enabled/active 状态。不得假设发布前 timer 已启用。

### 10.2 首次上线且不存在旧 release

如果发布前没有该服务：停止并禁用新 timer/Web，恢复或移除本需求的 Nginx include 和 units，然后 `daemon-reload`、`nginx -t` 并安全 reload。SQLite 和备份目录继续保留，不删除。

### 10.3 SQLite 数据回滚（仅必要时）

本节是同语义发布下覆盖/损坏数据库的灾难恢复，不是 D10 的标准回滚。D10 标准回滚直接把活动指针切回原样保留的历史 D30 库，不覆盖任何 SQLite。只有原 D30 库本身损坏且负责人明确批准时，才使用发布前 online backup：

1. 停止 timer、refresh 和 Web。
2. 先对当前 SQLite 再做一次 online backup，保留为回滚前证据。
3. 核验发布前备份 `PRAGMA quick_check` 和 SHA-256。
4. 在无进程访问数据库时恢复备份。
5. 启动旧 Web，核验旧 `data_version`、日期范围和 health。

不得直接删除 `/mnt/data-disk/dramawave-attribution-comparison`，不得把未校验的 `.sqlite3` 主文件覆盖到活跃 WAL 数据库。

## 11. 发布后记录

2026-08-06 D30 首次发布证据已回填到第 1、9.4 和 9.5 节，且仅作为历史基线。2026-08-07 D10 起点已批准为 `2026-08-01`，生产切换尚未执行。必须回填 D10 commit/release、独立候选 DB 路径、语义标记、D30 备份/回滚库、原子环境切换、真实点样本、自动化/浏览器、自然 timer、性能和回滚演练；在这些证据齐全前不得把状态改为已发布。

## 12. 变更记录

- 2026-08-07：部署目标由 D7/D30 改为 D7/D10，用户批准起点为 `2026-08-01`，边界调整后本地自动化 `63/63` 通过。保留 2026-08-06 D30 发布为历史基线；新增全新 D10 SQLite、语义门禁、旧 D30 库拒绝测试、活动 DB 指针原子替换和同时回滚到旧 D30 release/数据库的流程。候选和生产待验收。
