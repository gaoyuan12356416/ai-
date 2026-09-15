# 部署与回滚

## 拓扑

- CPU 43.166.187.96:/root/drama_material_service 是多功能后台组合目录。仅更新 worker、async_runtime、app_support、新增 prefetch 模块。
- HK 43.154.250.89:/data/drama-synthesis-gpu/current 使用完整发布目录；基线 df1c495545a78d25d313a5b3443d02a8fb46a759。
- CPU 备份和验证放在已挂载的 /mnt/data-disk；HK 放在 /data/drama-synthesis-gpu。

## 步骤

1. 本地通过后推送 GitHub；两端 fetch/checkout 完整提交并跑离线回归。
2. 备份 CPU 模块、unit/drop-in、在线 SQLite；备份 HK 发布指向、配置及 runtime JSON。工作和产物目录保留。
3. 配置 CPU 双观察、HK 预取开关及实测下载并发；同步完整 release SHA。
4. 窄范围重启 CPU API/job worker、HK GPU worker；验证反向隧道。
5. 核对原任务 ID/指纹、租约、进度时刻、阶段与文件增长。

## 回滚原则

停止相关服务；恢复 CPU 指定文件/配置，HK current 与 release SHA；移除本次新增 drop-in，daemon-reload 后启动服务和隧道。不要用旧数据库/runtime 快照覆盖已经前进的制作状态，不删除下载/分片/上传记录。

## 2026-09-15 已部署

- 生产代码提交：`82274708ebb750f6a1d15be3627e70aab5c11a60`；GitHub 分支 `codex/drama-throughput-20260915`。
- CPU 拉取检出：`/mnt/data-disk/drama-throughput-20260915/source`；备份 `/mnt/data-disk/drama-throughput-20260915/backups/pre-8227470`。
- HK 发布：`/data/drama-synthesis-gpu/releases/82274708ebb750f6a1d15be3627e70aab5c11a60`；备份 `/data/drama-synthesis-gpu/backups/20260915-throughput-pre-8227470`。
- CPU 新增 `drama-material-job-worker.service.d/99-drama-throughput.conf`，观察数量 2。
- HK 新增 `drama-synthesis-gpu-worker.service.d/99-drama-throughput.conf`，完整 release SHA、下载 6、预取 2、预算 16 GiB、空闲 20 GiB。
- CPU API/job worker、HK worker/tunnel 均已重启并 active/running；API topbar、公网页面、CPU 到 HK 的 healthz 均 HTTP 200。
- 原任务保留 ID、冻结输入指纹和已完成 44 集下载；GPU 代次从 1 到 2，CPU 自动接回原任务。

旧 GPU 优雅退出等待下载线程超过 120 秒。切换前暂停其主进程，确认操作系统与 runtime 均无子进程、无 YouTube 媒体文件句柄，核验 4 个部分下载文件的持久前缀 SHA，再结束旧服务。证明保存于 HK 备份内 `shutdown-checkpoint-proof.json`。重启后复核 44 个完整记录仍在；重新校验期间网络传输为零、无新增 FFmpeg 转码命令。

## 精确代码回滚

回滚前核对 manifest 和当前代码仍属于本次部署，避免覆盖后续变更。保留所有实时数据库与工作目录。

CPU：

```bash
systemctl stop drama-material-job-worker.service drama-material-api.service
cp -a /mnt/data-disk/drama-throughput-20260915/backups/pre-8227470/files/. /root/drama_material_service/
rm -f /root/drama_material_service/features/drama_synthesis/prefetch.py
rm -f /etc/systemd/system/drama-material-job-worker.service.d/99-drama-throughput.conf
systemctl daemon-reload
systemctl start drama-material-api.service drama-material-job-worker.service
```

HK（优先选择当前制作完成后的停机窗口）：

```bash
systemctl stop drama-synthesis-gpu-worker.service
test ! -e /data/drama-synthesis-gpu/current.rollback-8227470
ln -s /data/drama-synthesis-gpu/releases/df1c495545a78d25d313a5b3443d02a8fb46a759 /data/drama-synthesis-gpu/current.rollback-8227470
mv -T /data/drama-synthesis-gpu/current.rollback-8227470 /data/drama-synthesis-gpu/current
rm -f /etc/systemd/system/drama-synthesis-gpu-worker.service.d/99-drama-throughput.conf
systemctl daemon-reload
systemctl start drama-synthesis-gpu-worker.service drama-synthesis-gpu-tunnel.service
```
