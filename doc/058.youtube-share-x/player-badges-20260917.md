# YouTube 自动发布列表：播放器卡片标记

2026-09-17 15:27（北京时间）已上线。用户可以在原任务状态下方看到播放器卡片标记和检测时间，不再需要逐一打开转发弹窗检查。

- 绿色「可发播放器卡片」：页面提供同视频 Player Card，API 确认同频道、已公开、处理完成、允许嵌入，且未返回地区或年龄限制。
- 黄色「卡片播放受限」：有 Player Card，但 API 返回地区或年龄限制。
- 其他结果分别显示暂无播放器卡片、视频当前未公开、不可嵌入、待检测/复查、暂未确认。过期结果不保留绿色。
- 页面读取时自动排队，最多两个后台检测线程；15 分钟有效期、错误 2 分钟退避，有界单飞缓存。GET 请求不等待平台网络，缓存只在进程内，不写发布台账。
- 标记只表示检测时的条件，不能承诺 X 实际播放。实际转发仍执行原来的实时校验、权限和去重规则。

## 代码与部署

分支 `codex/youtube-share-x-20260916`；运行提交 `20f0b00461ab10b9da0659dfb4eb4478a2c2409c`。GitHub 推送与 CPU 拉取相同 SHA 已确认。

服务器 `43.166.187.96`，主运行目录 `/root/drama_material_service`，公开静态目录 `/usr/share/nginx/html`。精确更新六份主目录文件和三份 Nginx 静态文件；只有 `drama-material-api.service` 重启，PID 由 449708 变为 603482。YouTube worker PID 2955771、X sidecar PID 4055167 不变，三个服务 active/NRestarts=0。

备份 `/mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-player-badges-20260917-152704-20f0b00461ab`，含原始文件、manifest、result 和 verification。数据盘 UUID 已确认，部署前后逐文件 SHA 校验通过。

## 验证

- 本地 123 项通过：新标记后端 14、工作流 45、分享桥接 20、公共卡片解析 4、HTTP 21、前端 Node 19。Python 编译、Node 语法、diff 检查通过。
- CPU 在 GitHub 拉取目录运行相同五组 Python 用例，104 项通过。
- 使用现有有效飞书管理员会话读取真实任务接口，HTTP200。76 个可见任务中 67 个账本确认公开的视频完成自动检测：ready 33、restricted 2、not_player 28、not_public 1、unavailable 3。数量为本次验收时快照。
- 后台队列逐步从 67 项归零，用时约 50 秒；各次列表 GET 0.087–0.124 秒（服务器 loopback，不代表用户浏览器整页时间）。稳定结果使用 since 返回 unchanged，匿名同接口 HTTP401。
- 原图片卡片、已确认播放器、地区受限、当前不公开四类实际视频分别得到预期标记。
- 公网 HTML/JS/CSS 均 HTTP200，字节哈希匹配 GitHub 发布目录；页面脚本和样式版本 `20260917-player-badges-v1`。
- 准备表及通知表逐行摘要保持一致，X 分享 run/item/attempt 各 1 条且数量不变，没有新增 X 帖子。YouTube 运行账本在核查期间有既有预约任务更新时间变化，未声称整张运行账本保持不变；新模块没有数据库写入或发布调用。
- 实际浏览器验收因工具两次 request-header policy 初始化失败未完成。Node 已验证真实渲染函数的绿色判定、过期降级、提示逃逸及原弹窗交互，不将这些测试称为浏览器视觉验收。

## 回滚

以下命令已执行只读 `--check` 验证并通过；未实际回滚。恢复本次九个文件、删除本次新增模块并重启主 API，保留业务数据库与所有发布服务。发现后续版本文件漂移会拒绝覆盖。

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/youtube-player-badges-20f0b00461ab10b9da0659dfb4eb4478a2c2409c/scripts/deploy_youtube_player_badges.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-player-badges-20260917-152704-20f0b00461ab
```

技能上下文已同步到 `ai-backend-maintenance/references/youtube-share-x.md`；未修改记忆库。
