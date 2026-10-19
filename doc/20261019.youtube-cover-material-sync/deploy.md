# 部署与回滚
源分支 codex/youtube-cover-material-sync-20261019。必须先本地测试、GitHub push，再由CPU从GitHub fetch精确SHA；版本存放 /mnt/data-disk/deploy/youtube-cover-material-sync/releases/<SHA>。

在该release执行 `python3 ops/youtube-cover-material-sync/deploy.py`，保存返回backup。安装器不自动启动timer。独立状态 /mnt/data-disk/youtube-cover-material-sync；current软链 /opt/youtube-cover-material-sync/current。

以主API环境加载现有配置，执行 `sync.py plan --scope approved` 和 `sync.py freeze --scope approved`。冻结manifest/hash留档，运行小批并核对后 `systemctl enable --now youtube-cover-material-sync.timer`。

服务每轮最多50张，结束15秒后再运行；所有自动MySQL读取经过主机FIFO，单次8秒SQL、25秒进程，不绕过限流。大批历史回传不占YouTube发布worker。

回滚：`python3 <已部署release>/ops/youtube-cover-material-sync/deploy.py --rollback <安装器返回backup>`。仅停用/恢复此服务，保留同步SQLite、图片和已完成的素材库记录。不得还原主发布数据库，也不得删除已供优化师使用的素材。升级后漂移由安装器拒绝覆盖。

## 已部署版本
- CPU：43.166.187.96。
- 运行提交：5870e1126403263dcd1dbaf28e6cfe297dc43f29。
- 运行路径：/opt/youtube-cover-material-sync/current -> /mnt/data-disk/deploy/youtube-cover-material-sync/releases/5870e1126403263dcd1dbaf28e6cfe297dc43f29。
- 本次备份：/mnt/data-disk/deploy/youtube-cover-material-sync/backups/20261019T081359Z；初次安装前备份20261019T080230Z。
- 存量冻结：730张，729张横图、1张方图；manifest SHA256=3786ddabc8319bfa2649aa2ab1c87948426478683a2af47f289b82b8e6e755cf。
- 本地与Linux20项测试通过；真实首批3张正式入库后启用timer，发布worker及unified-writer未重启。

紧急停用同步（保留已入库图片、外部素材ID和全部本地账本）：
```bash
systemctl disable --now youtube-cover-material-sync.timer
systemctl stop youtube-cover-material-sync.service
```

撤销最近代码/配置更新（不会恢复旧账本，也不会自动重新启动旧timer）：
```bash
python3 /mnt/data-disk/deploy/youtube-cover-material-sync/releases/5870e1126403263dcd1dbaf28e6cfe297dc43f29/ops/youtube-cover-material-sync/deploy.py --rollback /mnt/data-disk/deploy/youtube-cover-material-sync/backups/20261019T081359Z
```

SQLite源目录允许创建读者WAL/shm旁文件，源数据库文件仍由ReadOnlyPaths强制只读，代码mode=ro/query_only不变。不得使用immutable读取活动库来绕过该问题。

最终验收：2026-10-19 16:33北京时间，冻结730张全部confirmed，729新上传+1已存在认领；零失败、零重复正式ID。完整回执见同目录test-report.md及服务器final-acceptance.json。
