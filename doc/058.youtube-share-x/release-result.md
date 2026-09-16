# 发布结果

代码已推送 `codex/youtube-share-x-20260916`，部署精确 commit `31cd6ceac6fdd60a1cbaade9f8a05dc090bc2e17`。基线同步独立提交 `854e8bd`，保留上线前已存在的素材筛选、任务列表性能、退休模块与 X duration routing 改动。

生产入口：[YouTube 自动发布](https://ai.yingliangads.com/youtube-publish.html)。主API/X sidecar均active且NRestarts=0；原YouTube worker PID2955771保持不变。五个原活动X触发器均已恢复active。新进程日志未发现Traceback/权限/导入/SQLite异常。

验证结果：

- 本地桥接17、Sidecar28、YouTube HTTP21、工作流45、素材筛选6、X应用契约30、UI17，共164项通过；Python编译、Node语法与git diff检查通过。
- CPU上用完整当前线上Sidecar依赖与新功能覆盖，在x-post-automation用户下运行：X账号68/68、分享服务28/28通过。排除了本地旧x_posts依赖造成的组合基线失败；未向生产下发旧依赖。
- 真实线上桥接只读读取20账号，19可选；六个宏返回完整，默认描述预览有效，权重155。
- 原视频`8XOlqX69rck`实时YouTube频道身份/public/processed检查通过；没有写入YouTube或X发布接口。
- 匿名新接口401；三份公开页面资源200且双静态目录与发布哈希一致。
- 独立分享台账run/item/attempt均为0：上线与验收没有新增X帖子。
- 浏览器线上匿名会话正确要求登录；弹窗视觉使用同一已部署静态资源的本地标记测试数据验收，不伪造生产登录。
- 浏览器实测选择2账号后编辑宏仍保留选择，点击剧名宏插入光标处并维持输入焦点；最终预览109/280且自动补充视频链接。停用账号与预约任务不可选/不可转发，弹窗正文可滚动、底部确认按钮可见。没有点击确认转发；验证结束已关闭本地夹具服务。

备份：`/mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-share-x-20260916-190533-31cd6ceac6fd`。含manifest、jobs/X账号在线SQLite备份、result.json、verification.json、source-verification.json。

回滚命令（保留数据库，需无待处理分享且文件无后续漂移）：

```sh
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/youtube-share-x-31cd6ceac6fdd60a1cbaade9f8a05dc090bc2e17/scripts/deploy_youtube_share_x.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-share-x-20260916-190533-31cd6ceac6fd
```

当前Sidecar：`/mnt/data-disk/x-post-automation/releases/31cd6ceac6fdd60a1cbaade9f8a05dc090bc2e17-youtube-share-x`；原release保留可回切。维护技能新增 `ai-backend-maintenance/references/youtube-share-x.md`，记载权限/宏/存储/恢复和部署约束。

实际卡片仍由YouTube/X控制；某些视频只有图片预览，部分访问环境会被YouTube登录/反机器人要求阻断。此功能使用原视频直链，不能强制平台渲染播放器。
