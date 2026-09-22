# 生产部署证据 · 2026-09-22

- 功能分支：codex/youtube-channel-templates-20260922。
- GitHub 运行提交：5f75aecb4877a9639b270898408b221cf52d2024；服务器 GitHub fetch 的 FETCH_HEAD 与运行提交一致。
- PR：https://github.com/gaoyuan12356416/ai-/pull/4 （基于已核对的 YouTube worker 分支，草稿待代码审阅）。
- 主机：43.166.187.96；运行目录 `/root/drama_material_service`；静态同时安装 `/usr/share/nginx/html`。
- 上线时间：2026-09-22 11:38:49 北京时间；变更18个目标文件。
- Release：`/mnt/data-disk/deploy/youtube-auto-publish/releases/5f75aecb4877a9639b270898408b221cf52d2024`。
- 备份：`/mnt/data-disk/deploy/youtube-auto-publish/backups/channel-templates-20260922-113849-5f75aecb4877`，包含在线SQLite备份、18项文件manifest及verification.json。

## 验证

- Linux Python 3.9：511项运行、510通过、1跳过（历史损坏图片fixture缺失）；含Linux进程组专项和3项回滚演练。
- 本地浏览器：模板20、预约38、轮询竞态10，共68个断言通过。隔离API与模拟保存/任务，未调用真实发布平台。
- 18项文件安装SHA全部匹配；公网6个HTML/JS/CSS均200且SHA匹配；频道列表、模板读取及原bootstrap匿名均401。
- 使用已有未过期普通用户会话（role=user），仅在进程内使用Cookie；频道列表GET200，83频道，核验完成，全部包含模板摘要，DTO没有凭证/scopes/内部授权账号ID。选定频道模板GET200，真实频道身份一致，version=0且三字段为空。原bootstrapGET200。
- 对备份中的174个准备任务逐一比较request_sha以及三项原模板和最终文案，全部不变。发布账本181→181、短链179→179、准备任务174→174。
- 生产模板表已初始化，行数0；没有为用户频道写入测试模板，没有创建视频/首评/审核通知。
- 主API PID 1163352→1223733、active；youtube-auto-publish-worker保持317191、active；统一writer保持2937249、active；旧publisher保持inactive。NRestarts均0。

## 精确回滚

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/5f75aecb4877a9639b270898408b221cf52d2024/scripts/deploy_youtube_channel_templates.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/channel-templates-20260922-113849-5f75aecb4877
```

回滚前校验当前文件仍为本次安装SHA。仅恢复代码/静态/导航并重启主API，保留SQLite模板行、发布账本、短链及素材。出现后续文件或导航漂移时脚本拒绝执行，需要重新做差异审查。不能恢复备份数据库覆盖上线后的真实状态。

技能上下文：已在 ai-backend-maintenance 的 references/youtube-auto-publish.md 追加本功能规则和部署记录指针；未改动记忆目录。
