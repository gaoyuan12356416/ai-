# YouTube 归因长链上线记录

上线时间：2026-09-14 17:20 北京时间。
运行提交：b4313da499c359fc7583441f24fdf9b3ea81824f。
GitHub 分支：codex/youtube-attribution-20260914。
CPU：43.166.187.96，代码目录 /root/drama_material_service。

新任务使用已确认的八参数长链。af_channel 从发布用户的租户内账号取 email，精确匹配 admin_user_group.email，取唯一正整数 sub_user_id。找不到映射或对应不同 ID 时明确报错。发布记录 ID 和时间戳取实际共享上传账本。每条新发布任务单独冻结短链，原素材来自合成任务也不会复用其他发布任务的归因。

本地与服务器分别执行 632 项测试：631 通过、1 项历史图片 fixture 条件跳过。分组：YouTube 419（1 跳过）、旧 YouTube 127、合成存储 86。新增归因专项 8 项全部通过，覆盖真实 SQLite、URL 编码、邮箱映射、不可领取未完成长链、磁盘失败恢复、写完短链后的响应丢失恢复、历史链接保留、同素材不同发布任务隔离。已有功能保护检查通过。

上线前用用户指定任务的实际邮箱和发布账本只读核验，新规则输出 af_channel=789，发布记录 ID=29，与确认示例一致。未创建真实测试视频、评论、消息或发布准备任务。

上线后：主 API、旧发布 worker、新自动发布 worker、统一同步 writer 均 active，NRestarts=0；前三个进程重新加载代码，统一 writer PID 保持 2994555，HK executor 未操作。网页 HTTP 200，匿名 API 401。SQLite quick_check=ok。原有 25 发布账本、25 短链、20 准备任务逐行与备份一致；示例历史公开短链 HTTP 200，HTML SHA 与账本完全一致。新增归因表 0 行，等待正常新任务。尚未使用新规则执行实际平台发布。

备份：/mnt/data-disk/deploy/youtube-auto-publish/backups/attribution-20260914-171954-b4313da499c3
发布包：/mnt/data-disk/deploy/youtube-auto-publish/releases/b4313da499c359fc7583441f24fdf9b3ea81824f

回滚命令（在 CPU 执行）：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/b4313da499c359fc7583441f24fdf9b3ea81824f/scripts/deploy_youtube_attribution.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/attribution-20260914-171954-b4313da499c3
```

回滚脚本拒绝覆盖后续代码变化、活动发布和未完成的新归因准备任务。只恢复代码，保留 SQLite、历史发布、封面、短链和通知记录。备份数据库仅供审计，不覆盖后续真实状态。

生效范围：部署后新建且模板含 {url} 的自动发布任务；历史及部署前已创建任务继续使用冻结的旧链接。审核期间短链地址已预留，审核通过并分配发布记录 ID 后正式生成跳转文件，生成成功才允许上传。
