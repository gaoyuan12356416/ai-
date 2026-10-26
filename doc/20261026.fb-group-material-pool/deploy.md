# 部署与回退

43.166.187.96：FB /opt/fb-auto-post/current；主API /root/drama_material_service；静态 /usr/share/nginx/html。备份/候选release位于/mnt/data-disk/fb-video-publish。

GitHub推送后服务器fetch精确提交，干净checkout执行scripts/deploy_fb_group_material_pool.py预检和--apply。校验数据盘UUID/空间、SHA及目标白名单、发布时点；排空旧生产者与租约、在线备份SQLite/文件manifest、迁移并比对历史记录，再窄重启和恢复原服务状态。新增fb-group-pool-scheduler.service，空池默认暂停。

回退：python3 <release>/scripts/deploy_fb_group_material_pool.py --rollback <backup>/manifest.json。

只关闭FB_VIDEO_GROUP_POOL_ENABLED与新调度；保留只读记录和身份入口权限、兼容后端、现库和全部短链。禁止恢复旧SQLite/旧执行器。准确路径在最终验收后追加。
