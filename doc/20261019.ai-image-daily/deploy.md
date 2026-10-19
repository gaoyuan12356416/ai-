# Dramawave AI 图片日报部署

## 部署边界与配置

- CPU：`43.166.187.96`；新增独立 `ai-image-daily.service` / `ai-image-daily.timer`，不修改或重启现有发布服务。
- 代码：`/mnt/data-disk/deploy/ai-image-daily/releases/<40位SHA>`；入口：`/opt/ai-image-daily/current`；状态、快照及公共首页备份：`/mnt/data-disk/ai-image-daily`。
- 只读复用 `/root/drama_material_service/.env`；不新增凭证，不改数据库结构，不修改素材、发布队列或 YouTube 同步账本。
- 源 SQLite 本体只读，所在目录允许 WAL/shm reader 旁文件。已部署的后台数据目录实际位于 `/mnt/data-disk/root-storage-20260915/rootfs/root/drama_material_service/data`；unit 同时覆盖逻辑路径和实际路径。YouTube 账本为 `/mnt/data-disk/youtube-cover-material-sync/sync.sqlite3`。
- 自动 MySQL 查询经 `/usr/bin/mysql` 全局 SQL Gate；允许 `/run/sql-connection-gate` 与 `/var/lib/sql-connection-gate/session-locks`，不绕过 Gate。
- timer 明确北京时间 09:00、09:10、09:30，`Persistent=true`；程序按报告日期成功后跳过。超时 1800 秒，进程锁避免手工运行和 timer 重叠。

## GitHub-first 安装

1. 本地完成测试和 `git diff --check`，只提交本需求文件并 push GitHub；记录完整 SHA。服务器核实 GitHub SSH 身份和仓库访问。
2. 核实 `/mnt/data-disk` 为真实挂载，UUID 为 `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`、可写且余量至少 1 GiB；挂载异常直接停止。
3. 在数据盘上的 Git 仓库 fetch 已 push 的提交，再 `git archive` 到全新的对应 SHA release 目录。不要覆盖已有 release，也不要通过 SCP 将本地未提交源码直接作为部署版本。若升级已有日报服务，先停止本日报 timer，等待在途日报完成；部署器会拒绝在 timer 或 service 活跃时切换代码。

以下为部署命令模板，`COMMIT`、`REPO`、`RELEASE` 必须使用现场核实值：

```bash
set -euo pipefail
COMMIT=<已push的40位SHA>
REPO=<数据盘上已存在的Git仓库路径>
RELEASE=/mnt/data-disk/deploy/ai-image-daily/releases/$COMMIT
git -C "$REPO" fetch origin
test "$(git -C "$REPO" rev-parse "$COMMIT^{commit}")" = "$COMMIT"
mkdir -p /mnt/data-disk/deploy/ai-image-daily/releases
mkdir "$RELEASE"
git -C "$REPO" archive "$COMMIT" | tar -x -C "$RELEASE"
python3 -B "$RELEASE/ops/ai-image-daily/deploy.py" \
  --repo "$REPO" --expected-commit "$COMMIT"
```

部署器逐项对比模块文件与 Git blob，检查 Python 语法，在私有临时目录中以 release 路径验证 systemd units，全部通过后才备份并安装。安装输出的 `backup` 是本次精确回滚点；其 `rollback.json` 记录旧代码目标、旧 units 是否存在、安装文件 hash 和 state 小文件名单。安装不自动启用/启动 timer。

4. 首次真实执行及核验后启用：

```bash
systemctl start ai-image-daily.service
systemctl show ai-image-daily.service -p Result -p ExecMainStatus -p ActiveState
systemctl enable --now ai-image-daily.timer
systemctl list-timers ai-image-daily.timer --all --no-pager
```

不要输出完整 `.env`、systemd environment 或含凭证的错误内容。若已有旧版日报服务正在执行，等本次完成后再切换 release，不中断它正在进行的公共入口发布。

## 验证和发布证据

- 保存 GitHub SHA、release 路径、安装 backup、数据盘挂载/余量、units 验证及 timer 下次北京时间触发记录。
- 校验首次报告日期为执行当天北京时间的前一天；10 月 19 日的存量补传属于 10 月 19 日入库，出现在 10 月 20 日日报。
- 保存数据读取、汇总勾稽、抽样素材 ID 指标核验和正式素材去重证据；失败读取不能渲染为零。检查近 7 天回补趋势和原始每日快照同时保留。
- COS 固定 key 为 `codex-artifacts/html/dramawave-ai-image-daily/index.html`。首轮发布后验证公网 GET、SHA256、报告日期、缓存策略、历史页/CSV及手机筛选搜索。返回真实可访问链接。
- 再运行同一 service，验证当日已成功时跳过：不增加数据库查询、不再次上传 COS、不覆盖当日原始快照。
- 检查服务日志不含凭证；检查原有发布及素材同步服务状态，与上线前只读基线对照。

## 回滚

使用本次安装输出的实际备份目录运行：

```bash
python3 -B <本次release>/ops/ai-image-daily/deploy.py \
  --rollback <本次backup目录>
```

部署器先检测当前 release 与 unit hash 未漂移，再仅执行 `systemctl disable --now ai-image-daily.timer` 和 `systemctl stop ai-image-daily.service`，恢复安装前 units 与代码指针并 daemon-reload。原先没有日报服务时移除新增 units/current；原先有旧版时恢复文件但不自动启动。若需恢复旧 timer，应检查旧版代码和对应公共页面后再明确启用。

回滚不还原任何数据库，不恢复 state 小文件，不删除原始快照和日报历史，不删除素材，也不影响其他 timers。COS 首页默认保留当前已发布版本；若页面本身需回退，在本日报 timer 已停用后运行：

```bash
python3 -B <本次release>/ops/ai-image-daily/report.py \
  --state /mnt/data-disk/ai-image-daily \
  --env-file /root/drama_material_service/.env \
  --restore-public <state/backups/本次发布备份目录>
```

恢复命令限定 state/backups 下的目录，验证旧文件 SHA256 与当前 COS 首页的 installed SHA256，一致后才恢复同一 `index.html` key，并公网 GET 比较 hash。无旧页或当前页出现漂移会拒绝恢复；不会删除 COS，也不会改写已归档日报。安装备份 `deploy/.../backups` 与公共页面发布备份 `state/backups` 用途不同，不可混用。

## 实施记录

本地部署器 Python 语法检查与 5 项防护检查通过；在 CPU 的数据盘临时目录执行 `systemd-analyze verify` 返回 0，无警告。三个 `systemd-analyze calendar` 均通过，下一触发为北京时间 2026-10-20 09:00、09:10、09:30；临时校验文件已清理，没有安装或启动服务。

实际 commit、服务器安装回滚点、首轮报告数据日期、COS 校验及 timer 状态由上线执行者在真实验证后补充，不能将本文命令模板视作已部署证据。
