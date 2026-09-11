# 部署与回滚

主机43.166.187.96；运行/root/drama_material_service；静态/usr/share/nginx/html。

脚本scripts/deploy_youtube_channel_cache.py复用备份/空闲/安装哈希校验。精确验证GitHub提交标记与生产文件基线；仅替换app.py中的YouTube handler，其他内容保持。改动文件备份与SQLite在线备份在数据盘。停止API及两个关联YouTube worker，统一writer保持运行；安装后恢复并健康检查。rollback参数指向本次backup目录，只恢复代码，保留所有任务、通知、人工频道核验审计、素材SQL与资产。

生产旧基线（同一登录用户、loopback HTTP）：topbar3.4ms/bootstrap3.5ms/tasks10.6ms/channels2644.5ms/materials冷4877.1ms/热3.9ms。这是服务器接口耗时，不代表用户网络与整页耗时。

## 保留当前生图的滚动切换

--drain-generator 只允许向前部署，仍拒绝活动发布账本，但允许生图/准备任务。已核对新worker只有After API，无Requires/BindsTo/PartOf，Restart=always；其SIGTERM handler仅设置STOP，当前subprocess.run完成后才退出。此模式只停止API和legacy worker，保持自动worker PID、子进程、任务lease；安装/健康验证全部通过后只给原Python MainPID发SIGTERM（不向cgroup或Codex子进程发信号）。当前工作自然完成后，systemd等待10秒自动以新代码重启。回滚仍使用默认保护且保留全部业务数据。


滚动竞态修正：停API/legacy后仅SIGSTOP旧Python MainPID，子进程继续；验证无Watchdog、PID不变及SQLite写锁可获得后再备份/替换。此时旧主进程不能领取下一项。成功后先排队SIGTERM再SIGCONT，只完成当前项后自动重启。任何失败恢复代码/服务后均SIGCONT，绝不遗留冻结进程；若暂停瞬间持有SQLite锁则拒绝部署并恢复进程，不进行备份以免死锁。

SIGSTOP后另验证/proc主进程State=T、有效generating lease以及直接Codex子进程；不满足则恢复进程并拒绝此次drain。jobs和failure outbox两份数据库均探测写锁，避免暂停状态下的备份死锁。

## 2026-09-11 11:18生产上线

GitHub分支codex/youtube-channel-cache-20260911，精确运行提交`b3679d355758d6f318fc0dd18ef4c949498cd336`，服务器fetch/归档/marker已核验。backup：`/mnt/data-disk/deploy/youtube-auto-publish/backups/channel-cache-20260911-111836-b3679d355758`。

准确回滚（等待当前生图或上传自然完成，脚本有活动任务保护，不恢复旧DB）：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/b3679d355758d6f318fc0dd18ef4c949498cd336/scripts/deploy_youtube_channel_cache.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/channel-cache-20260911-111836-b3679d355758
```

12文件安装/两份静态SHA匹配；app仅替换YouTube handler；API和legacy worker已重启active，统一writer PID2994555保持不变。旧auto worker MainPID3119019仍在完成既有生图，安装窗口只冻结主进程，Codex子进程未收到信号；安装后已排队SIGTERM+SIGCONT，当前项返回后systemd自动采用新版本。11:20核验其仍active，切换尚未发生。

切换前后发布账本8条（published4/unknown4）、准备任务5条（enqueue_pending1/generating1/uploading3）、短链10条相同；素材SQL摘要不变。无真实测试视频/首评/封面写入/人工核验确认。

11:25终验：旧worker于11:24:47正常退出，systemd于11:24:57自动启动新PID3658132（NRestarts=1）；API、legacy、auto worker和统一writer均active。原生图任务318d428a...本轮于11:24:45达到原有20分钟上限，不可变失败通知code=cover_generation_timeout；未向生图子进程发送部署信号。11:25:38任务又处于generating，为线上业务后续活动，本次验收未触发重试。原待入队任务2968aba...由新worker正常接续处理，后续业务变化不能算作部署测试。
