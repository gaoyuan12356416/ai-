# 部署和回滚

目标：43.166.187.96，/opt/fb-auto-post/current。静态文件同时更新 /usr/share/nginx/html 和 /root/drama_material_service/static。

先commit/push GitHub，再服务器fetch精确提交并通过本地/服务器测试。脚本scripts/deploy_fb_hit_material.py默认仅preflight；--apply核验数据盘UUID、17个目标文件基线、无active渲染/发布任务后，暂时停止6个FB调度/效果timer、备份SQLite/代码、仅重启fb-auto-post-service.service。完成后恢复原timer状态。

不修改主API、GPU服务、业务配置、模板版本或今晚frequency切换timer。部署/回滚不恢复历史SQLite覆盖新发布事实。

回滚：使用本次GitHub源码中的部署脚本 --rollback 指向本次备份manifest.json；校验文件未发生后续改动、无在途任务后恢复旧代码，保留当前数据库。

已于2026-10-10 16:24（北京时间）部署。

- GitHub源提交：d0477a65f0c7793f65e709711e4ffb268692db41，分支codex/fb-hit-material-publish-20261010；服务器fetch并checkout同一提交，17个目标文件逐一hash核验。
- 代码：43.166.187.96:/opt/fb-auto-post/current；只重启fb-auto-post-service.service，active/running，health live/prebuild均true。
- 备份：/mnt/data-disk/fb-hit-material-publish/backups/20261010T082417Z-d0477a65/manifest.json（旁有publisher.sqlite3及逐文件备份）。
- 部署前后全部fb_auto_*业务表行数/内容哈希一致，所有FB原有timer已恢复active，今晚frequency切换timer保持active。
- 4个公网静态资源HTTP200且字节hash与GitHub提交一致；未登录POST返回401；新接口非法ID测试400，未创建真实爆款运行。

精确回滚命令（在CPU服务器运行；有活动爆款任务时会拒绝，以保留其执行保护）：

```sh
python3 /mnt/data-disk/fb-hit-material-publish/releases/d0477a65f0c7793f65e709711e4ffb268692db41/scripts/deploy_fb_hit_material.py --rollback /mnt/data-disk/fb-hit-material-publish/backups/20261010T082417Z-d0477a65/manifest.json
```

只恢复本次备份代码并窄重启；不恢复旧SQLite覆盖任何新发布事实。

若新入口已创建仍在planned/ready/submitted/unknown等活动状态的任务，回滚脚本拒绝撤回其执行保护；须先对账完成这些任务，禁止恢复旧数据库或强制重发。
