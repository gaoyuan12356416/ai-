# 部署和回滚

目标：43.166.187.96，/opt/fb-auto-post/current。静态文件同时更新 /usr/share/nginx/html 和 /root/drama_material_service/static。

先commit/push GitHub，再服务器fetch精确提交并通过本地/服务器测试。脚本scripts/deploy_fb_hit_material.py默认仅preflight；--apply核验数据盘UUID、17个目标文件基线、无active渲染/发布任务后，暂时停止6个FB调度/效果timer、备份SQLite/代码、仅重启fb-auto-post-service.service。完成后恢复原timer状态。

不修改主API、GPU服务、业务配置、模板版本或今晚frequency切换timer。部署/回滚不恢复历史SQLite覆盖新发布事实。

回滚：使用本次GitHub源码中的部署脚本 --rollback 指向本次备份manifest.json；校验文件未发生后续改动、无在途任务后恢复旧代码，保留当前数据库。

生产提交、备份路径、实际验证结果将在部署后补充。

若新入口已创建仍在planned/ready/submitted/unknown等活动状态的任务，回滚脚本拒绝撤回其执行保护；须先对账完成这些任务，禁止恢复旧数据库或强制重发。
