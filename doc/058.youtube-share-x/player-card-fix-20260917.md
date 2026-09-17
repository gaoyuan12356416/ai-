# YouTube 播放卡片校验修正

2026-09-17 已部署。原转发逻辑验证了公开视频，但未验证其是否提供 Player Card；同样允许嵌入的公开视频可能只提供图片卡片。当前修正是发布前的严格校验和视频链接优先规则，不会把 YouTube 提供的图片卡片强制转换为播放器。

- 反馈视频的 YouTube API 为 public/processed/embeddable=true；CPU 和本机的 Twitterbot 请求均返回 `twitter:card=summary_large_image`。`youtu.be` 和 `feature=shared` 仍返回图片卡；embed 页面无卡片元数据。
- 先前成功样例仍返回 `twitter:card=player`，播放器和 canonical URL 均属于同一视频。
- 新弹窗明确显示检测结果，未确认播放器时禁用新提交，并提供「重新检查播放器」。创建请求再次检查，图片卡/身份不匹配/不可用均在入队前返回 409。已有 UUID 的提交结果查询保持原行为。
- 原视频直链作为正文第一个链接；省略视频宏时，若正文有其他链接则在文案前补上原视频直链。显式把视频链接写在其他链接之后会提示修改。推广短链宏、自定义文案及归因保持原值。
- 源元数据通过只能证明源页面提供播放器，X 缓存、客户端展示和最终播放仍由平台决定。此次没有创建新的 X 帖子，未声称做过新版实际发帖或播放验证。

代码分支 `codex/youtube-share-x-20260916`，上线 commit `c1fbce7e9e45dcc19340c94b1522c9fe23efc3ce`。已推送 GitHub，CPU 拉取并验证相同 SHA 后部署。

服务器 `43.166.187.96`，主站 `/root/drama_material_service`，静态目录 `/usr/share/nginx/html`。仅重启 `drama-material-api.service`，新 PID 449708；X sidecar PID 4055167、YouTube worker PID 2955771 保持运行。三项均 active，NRestarts=0。

验证：卡片检查 4/4、bridge 20/20、UI 17/17、YouTube HTTP 21/21，共 62 项通过；Python 编译、Node 语法和 diff 检查通过。CPU GitHub 拉取版本验证反馈视频被拒、原成功样例通过。部署后真实 options 返回 not_player；受控 create 检查返回 409，禁止真实 create 的拦截器证明未调用入队；公开 HTML/JS HTTP 200 且哈希匹配；回滚预检查通过。

截图测试帖已按用户明确指令定点删除。删除前检查账号、作者、帖子 ID 和两条原始链接；一次 DELETE 返回 HTTP 200、`deleted=true`。后续 GET 外层 HTTP 200，响应 `errors` 明确为目标 ID 的 `resource-not-found`，已确认删除。没有重复 DELETE，也没有替代发帖。原分享 run/item/attempt 各 1 条，历史保留；删除前台账备份、公开帖子快照和删除证据保存在 CPU 数据盘 `x-post-automation/operator-actions` 的本次独立目录中。

备份：`/mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-share-player-20260917-113830-c1fbce7e9e45`，包含七个目标文件的基线/新版本哈希及 result/verification。回滚命令只恢复本次主站代码并重启 API，不恢复已删除的帖子、不重发任何任务：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/youtube-share-player-c1fbce7e9e45dcc19340c94b1522c9fe23efc3ce/scripts/deploy_youtube_share_player.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-share-player-20260917-113830-c1fbce7e9e45
```

加 `--check` 为只读回滚条件检查；该检查已通过，未实际回滚。参考官方接口：[创建帖子](https://docs.x.com/x-api/posts/create-post)、[删除帖子](https://docs.x.com/x-api/posts/delete-post)。
