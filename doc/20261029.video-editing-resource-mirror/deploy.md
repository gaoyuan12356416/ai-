# 发布与兼容回退

主分支codex/video-editing-resource-mirror-20261029；GPU复合分支codex/video-editing-resource-mirror-gpu-20261029，从实际05f48e2175c691534a88549a694cfe9d4cc6739d派生，仅覆盖recipe.py、render.py、gpu_integration.py及发布脚本/说明。

CPU /root/drama_material_service是复合运行目录，只安装限定功能文件和两份video-editing静态资源。SQLite仍位于/mnt/data-disk/video-editing/state.sqlite3；业务MySQL不DDL。先GitHub推送确切提交，再服务器fetch/checkout。

使用scripts/deploy_video_editing_resource_mirror.py执行两端只读check；CPU prepare关闭新剪辑入口并排空相关队列，GPU prepare/apply安装兼容渲染器，CPU apply安装接口和页面，最后GPU restore、CPU restore恢复原入口状态。入库未知账本保留读回核实，不重发。只有相关worker/API按需重启，不要求无关CPU任务清空。

出现新任务后回退只收拢新入口，保留v2和旧profile兼容读取器及数据库/COS。不得恢复旧SQLite快照或覆写历史任务。

2026-10-29发布完成：CPU功能提交b6cd305bc7f83746ab581e3c2cd3a60c5b6461f2；GPU复合提交6f9190e8487a37315e94103e9c778f1abfa9fee1。后续提交只补充文档，不替换本次运行代码。GitHub PR #23。

- CPU 43.166.187.96：运行目录/root/drama_material_service，静态资源另有/usr/share/nginx/html副本；GitHub发布检出/mnt/data-disk/video-editing/releases/b6cd305bc7f83746ab581e3c2cd3a60c5b6461f2。
- CPU备份/mnt/data-disk/video-editing/backups/resource-mirror-b6cd305bc7f8，包含目标文件、SQLite一致性备份、两次迁移演练、旧账本哈希及各阶段回执。
- GPU 43.154.250.89：current指向/data/drama-synthesis-gpu/releases/6f9190e8487a37315e94103e9c778f1abfa9fee1；原版本05f48e2175c691534a88549a694cfe9d4cc6739d保留。
- GPU备份/data/drama-synthesis-gpu/deploy-backups/resource-mirror-6f9190e8487a，包含配置、旧链接、预检和apply/restore回执。

实际顺序：两端check → CPU prepare → GPU prepare/apply → 隔离NVENC canary → CPU apply → GPU restore → CPU restore。GPU完整缓存校验及启动前校验较耗时，均成功完成。CPU只重启drama-material-api.service并停止/恢复两个剪辑相关worker；GPU只切换drama-synthesis-gpu-worker.service，tunnel保持active。无关CPU服务PID未变化。

发布后验证：CPU14份目标文件哈希匹配；SQLite新字段存在；旧task/output/material账本不变；所有相关服务active；两端剪辑入口恢复；GPU SAFE_HEALTH及旧账本保持。实际NVENC镜像/字幕/淡化验证和生产页面验收见test-report.md。

兼容回退程序（仅需回退时执行）：在CPU以root执行以下命令，关闭新上传/校验/制作入口，继续保留已接受任务的CPU/GPU worker和所有新版读取能力，无需重启：

```sh
test -e /etc/video-editing/intake.disabled || printf '%s\n' 'resource-mirror rollback: pause new intake; retain v2 readers and accepted jobs' > /etc/video-editing/intake.disabled
```

随后逐个核实已接受任务原ID与回执；未知结果保留核实状态，禁止重发。修复后确认暂停文件确为本次回退创建，再删除该文件恢复入口。不要切回旧GPU读取器、恢复旧SQLite快照或删除成片。
