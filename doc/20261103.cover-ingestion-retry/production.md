# 生产验收与回滚

2026-11-03 北京时间 14:18 验收完成。运行代码提交：
`603f443fa13de600a9f2ff3ca11a0603e85b259a`，分支
`codex/cover-ingestion-retry-20261103`。已验证 GitHub push、服务器 fetch、Git blob 与 release 一致。

服务器 `43.166.187.96`，运行目录 `/root/drama_material_service`。
只替换 `features/cover_material_source.py` 和 app 中三个回调/报错函数；其余 app AST 保持不变。
主 API PID 从 2281641 变为 961298，四个截图生成服务和视频 worker 的 PID 均未改变。
内网 `/api/ui/topbar` 与公网 `/api/auth/status` 均返回 200。

本地与 CPU 均通过三个离线测试命令（分别执行 66、59、6 项，包含重用的回归用例）。
覆盖 COS 有限重试、部分流丢弃、不可重试错误、入库阶段分类、已成功和未知结果不重复提交、
只替换目标函数、恢复时回执/任务行不被覆盖。生产函数离线渲染也确认旧故障显示为素材入库失败，
真实生成超时仍显示为图片生成失败；此检查不发送飞书通知。

指定任务已恢复 `done / 100%`，原错误已清除。三图 SHA256 均未变化。
本次只新增两次入库 POST，原方图成功账本行完全不变。三种尺寸均各有一次出站事件、
有效接收回执和正式素材确认；未重新生成或重复提交成功项。具体任务及素材 ID 保存在私有证据中。

代码备份：`/mnt/data-disk/ai-cover-material-source/cos-retry-20261103/code-backup`。
任务、账本及图片证据：`/mnt/data-disk/ai-cover-material-source/cos-retry-20261103/recovery`。
最终核验：`/mnt/data-disk/ai-cover-material-source/cos-retry-20261103/verified-recovery.json`。

## 精确代码回滚

先排空截图生成任务，再执行：

```sh
systemctl stop drama-material-api.service
python3 /mnt/data-disk/deploy/cover-ingestion-retry/releases/603f443fa13de600a9f2ff3ca11a0603e85b259a/ops/cover-ingestion-retry/deploy.py rollback --backup /mnt/data-disk/ai-cover-material-source/cos-retry-20261103/code-backup
systemctl start drama-material-api.service
```

回滚核对两个目标文件及备份 SHA，只恢复代码。保留现有 SQLite、三张图片、已入库素材和成功回执，
不重新上传。其他代码漂移时须重新核对，不能覆盖后续版本。
