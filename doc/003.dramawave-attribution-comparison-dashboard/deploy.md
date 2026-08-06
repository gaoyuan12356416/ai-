# 部署文档

## 1. 发布状态回填

本文件描述发布步骤，不代表已经发布。完成线上发布后再填写下表。

| 项目 | 实际值 |
| --- | --- |
| GitHub 目标 commit | 待发布回填 |
| 发布分支 / PR | 待发布回填 |
| 发布主机 | 待发布回填 |
| 发布时间（Asia/Shanghai） | 待发布回填 |
| 发布 release 路径 | 待发布回填 |
| 发布前 `current` 指向 | 待发布回填 |
| 发布前备份路径 | 待发布回填 |
| 首次 bootstrap 版本 | 待发布回填 |
| 首次 bootstrap 日期范围 | 待发布回填 |
| 第一轮自然 timer 时间及版本 | 待发布回填 |
| 回滚点 | 待发布回填 |

## 2. 固定拓扑

| 项目 | 固定值 |
| --- | --- |
| 代码发布根目录 | `/opt/dramawave-attribution-comparison` |
| 当前版本软链接 | `/opt/dramawave-attribution-comparison/current` |
| Web 监听 | `127.0.0.1:8832` |
| 公网页面 | `/reports/dramawave-attribution-comparison/` |
| SQLite | `/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3` |
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

`dashboard.env` 至少包含：

```dotenv
DRAMAWAVE_ATTRIBUTION_DB_PATH=/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3
```

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

原子切换 `current`：

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

此时不要先启 Web 或 timer。`service.py` 在缓存文件不存在时会退出码 2；应先完成首次 bootstrap。

## 7. 首次 bootstrap

### 7.1 日期范围

首次上线显式回填从 `2026-07-29` 到北京当天：

```bash
BOOTSTRAP_START='2026-07-29'
BOOTSTRAP_END="$(TZ=Asia/Shanghai date +%F)"
```

脚本拒绝未来日期，并自动删除/忽略 60 天保留窗口之外的日期。上线时若 `2026-07-29` 已超出保留窗口，实际起点以 `max(2026-07-29, 北京当天-59天)` 为准。

### 7.2 使用临时 systemd oneshot 执行

不要在交互 shell 中打印或手工拼接 MySQL 密钥。使用两个现有 `EnvironmentFile` 运行一次性 bootstrap：

```bash
BOOTSTRAP_UNIT="dramawave-attribution-comparison-bootstrap-$(date +%Y%m%d%H%M%S)"
systemd-run --unit="$BOOTSTRAP_UNIT" --wait --collect --pipe \
  --property=Type=oneshot \
  --property=WorkingDirectory=/opt/dramawave-attribution-comparison/current \
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
  --property=ReadOnlyPaths=/opt/dramawave-attribution-comparison/current \
  --property=ReadOnlyPaths=/root/drama_material_service/.env \
  --property=ReadWritePaths=/tmp \
  --property=ReadWritePaths=/mnt/data-disk/dramawave-attribution-comparison \
  /usr/bin/flock -E 75 -xn /tmp/tt_minis_multi_dim_dashboard.lock \
  /opt/dramawave-attribution-comparison/venv/bin/python \
  /opt/dramawave-attribution-comparison/current/refresh_cache.py \
  --bootstrap-start "$BOOTSTRAP_START" --bootstrap-end "$BOOTSTRAP_END"
```

执行前必须确认现有 TT 进程已经退出且共享锁空闲；非阻塞 flock 在锁被占用时以专用退出码 `75` 拒绝启动，不得移除锁后重试。bootstrap 受与定时 refresh 相同的 1GB cgroup 硬上限保护，并显式保持主机 `/tmp` 可见、可创建锁文件，以确保共享锁真实生效。bootstrap 只有全部目标日期完成 staging 后，才会在一个 `BEGIN IMMEDIATE` 事务中替换事实数据并推进 `data_version`。任一天源查询或映射校验失败都会清除本轮 staging、把已创建日志标成 failed，并保留旧事实和旧版本。

成功后核验：

```bash
DB=/mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3
sqlite3 "$DB" 'PRAGMA quick_check;'
sqlite3 "$DB" "SELECT MIN(dt),MAX(dt),COUNT(*) FROM attribution_fact;"
sqlite3 "$DB" "SELECT 'fact',COUNT(*) FROM attribution_fact UNION ALL SELECT 'filter_daily',COUNT(*) FROM attribution_filter_daily UNION ALL SELECT 'campaign_daily',COUNT(*) FROM attribution_campaign_daily;"
sqlite3 "$DB" "SELECT key,value FROM cache_meta WHERE key IN ('data_version','rollup_version','generated_at','last_refresh_dates','source_max_updated_at') ORDER BY key;"
sqlite3 "$DB" "SELECT dt,status,fact_rows,data_version FROM refresh_log ORDER BY id DESC LIMIT 20;"
```

要求：`quick_check=ok`、日期边界正确、三层均有数据、所有 bootstrap 日期日志为 success、`data_version` 非空且与 `rollup_version` 完全相等。把版本和实际范围回填到本文顶部。

## 8. 启动 Web、Nginx 和 timer

```bash
systemctl enable --now dramawave-attribution-comparison.service
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

最后启用 timer：

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
```

验证 `ok=true`、`stale=false`、缓存起点不早于 `2026-07-29`、终点为北京当天，并检查 `Cache-Control`、`ETag`。使用 meta 返回的日期和 `data_version` 调用 `/api/options`、`/api/query?include_rankings=0`、`/api/rankings`、完整兼容 `/api/query` 和 `/api/export.csv`；把旧版本传给 query/rankings，确认返回 409。

对当前真实全范围和 7/30/60 天可用范围分别执行默认、单渠道、单投放产品、单国家组、单优化师及 Ad Set 分组的 LRU-miss/同键热查询。若真实历史尚不足 30/60 天，用同量级合成 SQLite 补足容量门禁，但仍必须单独记录真实全范围结果。主查询（`include_rankings=0`）冷 p95 必须不高于 1 秒、热 p95 不高于 300 毫秒；排行异步加载，不得阻塞主表首屏，并单独记录耗时。

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
sqlite3 /mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3 \
  "SELECT key,value FROM cache_meta WHERE key IN ('data_version','rollup_version','generated_at','last_refresh_dates','history_cursor') ORDER BY key;"
sqlite3 /mnt/data-disk/dramawave-attribution-comparison/cache/dashboard.sqlite3 \
  "SELECT dt,status,fact_rows,data_version FROM refresh_log ORDER BY id DESC LIMIT 6;"
```

要求本轮为 success、`last_refresh_dates` 含今天/昨天/一个历史日期、版本已推进、Web health 显示新版本。将实际 timer 时间、run 日志和新版本回填为“第一轮自然 timer 证据”：待发布回填。

## 10. 精确回滚

### 10.1 代码/配置回滚（默认）

默认回滚不删除或回退 SQLite，先保留新数据用于审计。执行顺序：

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

代码回滚通常不需要恢复数据。只有旧代码确认无法读取新 SQLite schema，且负责人明确批准数据回退时，才使用发布前 online backup：

1. 停止 timer、refresh 和 Web。
2. 先对当前 SQLite 再做一次 online backup，保留为回滚前证据。
3. 核验发布前备份 `PRAGMA quick_check` 和 SHA-256。
4. 在无进程访问数据库时恢复备份。
5. 启动旧 Web，核验旧 `data_version`、日期范围和 health。

不得直接删除 `/mnt/data-disk/dramawave-attribution-comparison`，不得把未校验的 `.sqlite3` 主文件覆盖到活跃 WAL 数据库。

## 11. 发布后记录

发布完成后补齐：

- GitHub commit、PR、release 路径和发布前 `current`。
- 备份绝对路径、manifest 校验结果、SQLite `quick_check`。
- bootstrap 范围、事实行数、`data_version`、三源最大更新时间。
- Web PID、8832 监听、Nginx `-t`、飞书登录浏览器结果。
- 第一轮自然 timer 的开始/结束时间、刷新日期、版本变化和日志结果。
- 可执行的精确回滚 release 与备份路径。

在这些证据回填前，不得把本文标记为“已部署”或“生产验证完成”。
