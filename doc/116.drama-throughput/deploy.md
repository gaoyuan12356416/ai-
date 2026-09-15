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

精确提交、备份目录、命令和生产验收上线后追加。
