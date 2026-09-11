# 部署与回滚

主机43.166.187.96；运行/root/drama_material_service；静态/usr/share/nginx/html。

脚本scripts/deploy_youtube_channel_cache.py复用备份/空闲/安装哈希校验。精确验证GitHub提交标记与生产文件基线；仅替换app.py中的YouTube handler，其他内容保持。改动文件备份与SQLite在线备份在数据盘。停止API及两个关联YouTube worker，统一writer保持运行；安装后恢复并健康检查。rollback参数指向本次backup目录，只恢复代码，保留所有任务、通知、人工频道核验审计、素材SQL与资产。

生产旧基线（同一登录用户、loopback HTTP）：topbar3.4ms/bootstrap3.5ms/tasks10.6ms/channels2644.5ms/materials冷4877.1ms/热3.9ms。这是服务器接口耗时，不代表用户网络与整页耗时。

## 保留当前生图的滚动切换

--drain-generator 只允许向前部署，仍拒绝活动发布账本，但允许生图/准备任务。已核对新worker只有After API，无Requires/BindsTo/PartOf，Restart=always；其SIGTERM handler仅设置STOP，当前subprocess.run完成后才退出。此模式只停止API和legacy worker，保持自动worker PID、子进程、任务lease；安装/健康验证全部通过后只给原Python MainPID发SIGTERM（不向cgroup或Codex子进程发信号）。当前工作自然完成后，systemd等待10秒自动以新代码重启。回滚仍使用默认保护且保留全部业务数据。


滚动竞态修正：停API/legacy后仅SIGSTOP旧Python MainPID，子进程继续；验证无Watchdog、PID不变及SQLite写锁可获得后再备份/替换。此时旧主进程不能领取下一项。成功后先排队SIGTERM再SIGCONT，只完成当前项后自动重启。任何失败恢复代码/服务后均SIGCONT，绝不遗留冻结进程；若暂停瞬间持有SQLite锁则拒绝部署并恢复进程，不进行备份以免死锁。

SIGSTOP后另验证/proc主进程State=T、有效generating lease以及直接Codex子进程；不满足则恢复进程并拒绝此次drain。jobs和failure outbox两份数据库均探测写锁，避免暂停状态下的备份死锁。
