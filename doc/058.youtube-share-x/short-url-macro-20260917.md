# 推广短链宏上线记录

2026-09-17 已上线。刷新 `https://ai.yingliangads.com/youtube-publish.html` 后，转发弹窗可点击「推广短链 `{short_url}`」。取当前任务冻结的 `material.macro_url`，预览和实际提交共用相同渲染方法；未填视频链接时仍自动补充 YouTube 原链接。缺少短链时给出缺值错误，默认文案不受影响。

- 分支：`codex/youtube-share-x-20260916`，代码 commit：`163b7324ac331ba72550813017c46fb4701c6506`，已推送并由 CPU 从 GitHub 拉取该精确 commit。
- 服务器：`43.166.187.96`；主站 `/root/drama_material_service`，公开静态资源 `/usr/share/nginx/html`。
- 本地验证：`python scripts/test_youtube_share_x.py` 19/19；`node --test scripts/test_youtube_share_x_ui.js` 17/17；Python 编译、Node 语法和 `git diff --check` 通过。
- 发布前在 GitHub 拉取版本上只读验证线上任务：7 个宏，短链等于任务冻结值，标题/推广短链/视频直链合计 180/280 权重；禁止网络的预览检查通过。
- 发布后用已部署 bridge 读取真实任务和本人账号范围：options/preview 通过，宏共 7 个，短链内容与发布前一致。公开 HTML/JS HTTP 200，字节 SHA256 与已部署静态文件完全匹配。
- 只重启 `drama-material-api.service`，PID 4055169 → 419615，active，NRestarts=0。X sidecar PID 4055167、YouTube worker PID 2955771 均保持不变，active，NRestarts=0。
- X 分享 run/item/attempt 均为 0；无真实 X 发帖，无短链写入，无数据库迁移。

发布脚本：`/mnt/data-disk/deploy/youtube-auto-publish/releases/youtube-share-short-url-163b7324ac331ba72550813017c46fb4701c6506/scripts/deploy_youtube_share_short_url.py`。

备份：`/mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-share-short-url-20260917-105220-163b7324ac33`，含六个目标文件、manifest、result、verification。回滚条件预检查通过。回滚保持台账和 sidecar/worker 进程，仅还原本次主站文件并重启 API；执行：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/youtube-share-short-url-163b7324ac331ba72550813017c46fb4701c6506/scripts/deploy_youtube_share_short_url.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/youtube-share-short-url-20260917-105220-163b7324ac33
```

加 `--check` 可只验证回滚条件；已执行该检查，未实际回滚。
