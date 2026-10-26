# 部署与回退

43.166.187.96：FB /opt/fb-auto-post/current；主API /root/drama_material_service；静态 /usr/share/nginx/html。备份/候选release位于/mnt/data-disk/fb-video-publish。

GitHub推送后服务器fetch精确提交，干净checkout执行scripts/deploy_fb_group_material_pool.py预检和--apply。校验数据盘UUID/空间、SHA及目标白名单、发布时点；排空旧生产者与租约、在线备份SQLite/文件manifest、迁移并比对历史记录，再窄重启和恢复原服务状态。新增fb-group-pool-scheduler.service，空池默认暂停。

回退：python3 <release>/scripts/deploy_fb_group_material_pool.py --rollback <backup>/manifest.json。

只关闭FB_VIDEO_GROUP_POOL_ENABLED与新调度；保留只读记录和身份入口权限、兼容后端、现库和全部短链。禁止恢复旧SQLite/旧执行器。

## 2026-10-26 上线记录

- 部署提交：e59fd0b7cff057eee117fd680ade53d7e095e0aa。
- Release：/mnt/data-disk/fb-video-publish/releases/group-pool-e59fd0b。
- 文件与 SQLite 备份：/mnt/data-disk/fb-video-publish/backups/group-pool-20261026T091436Z-e59fd0b7/。
- 迁移后 6 张新表为空；11 张既有表冻结期间内容哈希完全一致；37 个文件匹配。5 个相关常驻服务 active，主 app.py 不变。
- 入口：https://ai.yingliangads.com/fb-post-material-pool.html；当前为空池、暂停，未保存发布配置、未创建测试帖子。

功能回退命令（需要回退时执行）：

```sh
python3 /mnt/data-disk/fb-video-publish/releases/group-pool-e59fd0b/scripts/deploy_fb_group_material_pool.py --rollback /mnt/data-disk/fb-video-publish/backups/group-pool-20261026T091436Z-e59fd0b7/manifest.json
```

回退保留只读入口以查看历史记录；新写入和组池调度关闭，已接受任务继续由兼容执行器处理。回退前脚本核对文件无漂移及旧池调度窗口。
