# 部署与回滚
源分支 codex/youtube-cover-material-sync-20261019。必须先本地测试、GitHub push，再由CPU从GitHub fetch精确SHA；版本存放 /mnt/data-disk/deploy/youtube-cover-material-sync/releases/<SHA>。

在该release执行 `python3 ops/youtube-cover-material-sync/deploy.py`，保存返回backup。安装器不自动启动timer。独立状态 /mnt/data-disk/youtube-cover-material-sync；current软链 /opt/youtube-cover-material-sync/current。

以主API环境加载现有配置，执行 `sync.py plan --scope approved` 和 `sync.py freeze --scope approved`。冻结manifest/hash留档，运行小批并核对后 `systemctl enable --now youtube-cover-material-sync.timer`。

服务每轮最多50张，结束15秒后再运行；所有自动MySQL读取经过主机FIFO，单次8秒SQL、25秒进程，不绕过限流。大批历史回传不占YouTube发布worker。

回滚：`python3 <已部署release>/ops/youtube-cover-material-sync/deploy.py --rollback <安装器返回backup>`。仅停用/恢复此服务，保留同步SQLite、图片和已完成的素材库记录。不得还原主发布数据库，也不得删除已供优化师使用的素材。升级后漂移由安装器拒绝覆盖。
