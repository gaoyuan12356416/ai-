# Dramawave AI 图片素材分析日报

独立只读日报：关联 AI 后台图片任务、上传回执及 YouTube 封面同步账本，使用正式素材 ID 汇总昨日产出与全部历史图片的昨日投放效果。业务日沿用后台日表 `dt`，不重算广告账号时区；北京时间每天 09:00 更新前一天报告。

## 运行入口

```bash
/usr/bin/python3 -B /opt/ai-image-daily/current/ops/ai-image-daily/report.py \
  --state /mnt/data-disk/ai-image-daily \
  --env-file /root/drama_material_service/.env
```

程序负责进程锁、按报告日期成功后跳过、只读数据查询、校验、每日快照和 COS 发布；成功后的同日再次触发不重复查询或上传。`ai-image-daily.timer` 使用 `Asia/Shanghai` 明确配置每天 09:00、09:10、09:30，后两次用于有限补偿；`Persistent=true` 允许停机恢复后补触发。

运行前检查数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`、可写性及至少 1 GiB 可用空间；失败直接退出，不降级写根盘。服务超时 1800 秒。

## 存储和查询边界

| 用途 | 位置 |
| --- | --- |
| 精确 SHA 代码 | `/mnt/data-disk/deploy/ai-image-daily/releases/<40位SHA>` |
| 代码入口 | `/opt/ai-image-daily/current`，指向 release 的符号链接 |
| 本地日报状态、快照、公共入口备份 | `/mnt/data-disk/ai-image-daily` |
| 安装回滚点 | `/mnt/data-disk/deploy/ai-image-daily/backups/<UTC时间>` |
| 后台源 SQLite | `/root/drama_material_service/data/drama_material_jobs.sqlite3` |
| YouTube 入库账本 | `/mnt/data-disk/youtube-cover-material-sync/sync.sqlite3` |
| COS 固定入口 | `codex-artifacts/html/dramawave-ai-image-daily/index.html` |

凭证只从已有后台 `.env` 读取，不写入源码、页面、命令参数或日志。MySQL 自动查询必须经 `/usr/bin/mysql` 的 SQL Gate，使用只读端点；不得使用直接驱动、`mysql.real` 或绕过 Gate 的环境配置。

systemd 对源 SQLite 本体强制只读，源目录仅为 SQLite WAL reader 的旁文件提供必要访问；SQL 同时使用只读连接与 `query_only`。服务仅拥有新增日报目录及上述旁文件、SQL Gate socket/锁目录的写权限，不修改发布队列。

## 安装和回滚

安装命令要求服务器已有从 GitHub fetch 的仓库，以及完整 commit SHA。部署器将 release 目录内的模块文件与该 commit 的 Git blob 逐项比较，编译检查 Python，并在安装前静态校验 systemd units。

```bash
python3 -B <release>/ops/ai-image-daily/deploy.py \
  --repo <服务器已fetch的Git仓库> --expected-commit <40位SHA>
```

安装会备份旧 units、旧代码指针和 state 根目录下不超过 2 MiB 的 JSON 小文件；不会启动或启用 timer。安装后先运行一次服务并完成真实数据/COS 验证，再 `systemctl enable --now ai-image-daily.timer`。

```bash
python3 -B <本次release>/ops/ai-image-daily/deploy.py \
  --rollback <本次安装返回的backup目录>
```

回滚只停用新增日报 timer/service、恢复旧 units 和旧代码指针。检测到更新的 release 或 unit 漂移时拒绝覆盖。日报历史、状态及已发布的 COS 页面全部保留。

确需恢复前一版 COS 首页时，在 timer 已停用后使用本次 release 的报告程序：

```bash
python3 -B <本次release>/ops/ai-image-daily/report.py \
  --state /mnt/data-disk/ai-image-daily \
  --env-file /root/drama_material_service/.env \
  --restore-public <state/backups/本次发布备份目录>
```

恢复前必须通过旧文件 SHA256 与当前 COS 页面 installed SHA256 校验；无旧页面或发现漂移时拒绝恢复，不删除 COS。归档日报和本地历史仍保留。

完整上线、验证和公共入口回滚步骤见 [部署文档](../../doc/20261019.ai-image-daily/deploy.md)。
