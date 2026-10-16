# TT 核对循环修复

旧任务持续返回 processing 时，旧执行器立即再次领取，并让发布循环一直不退出；正常制作每轮结束后要等 systemd 超时才能开始下一轮。

- processing 核对结果等待五分钟；领取查询同时尊重 reconciling / unknown 的 next_attempt_at_utc。
- 不同账号的 ready 优先；同账号有未知发布结果仍保持隔离。
- runner 的发布通道每轮最多 max(8, worker_count × max_tasks_per_worker × 2) 次请求，随后等待本轮制作结束并退出。
- 可配置 TT_AUTO_POST_EXECUTION_NOT_BEFORE_UTC。早于该时刻的任务禁止自动选材、制作及新发布；旧 publishing/unknown/reconciling 只能核对已有结果。旧的零发布尝试、无结果风险、无有效租约任务不占用新计划账号；未知结果和已有尝试继续保留账号门禁。
- 本次生产值 2026-10-15T16:00:00+00:00，即北京时间 10 月 16 日 00:00；保留旧任务、素材占用和发布账本，不取消、不重置、不重试历史失败。

本次基础版本为 68ba8bffb5f6862efd4a9315c3f8174adca9f7d1，仅替换 core.py、publisher.py、service.py 和 runner。生产文件有 CRLF；部署前同时核对原始 SHA256 和规范化后的 Git 源码。

验证：102 项 TT store/publisher/runner/service/health/selector 离线回归，包括持续 processing 有界退出、五分钟退避、未知写入不重发、历史 ready 保留但不执行、新日同账号隔离。真实发布不作为部署测试。

部署必须先保存定时器状态和 SQLite 在线备份，暂停 TT runner/scheduler；确认无 selecting/preparing/publishing 活动后切换，仅重启 TT Auto sidecar。香港 TT random/direct GPU 来源域名仅追加 socialkit-cdn.yingliang.tech，保留现有 COS 域名和跳转检查。

回滚源码时必须保持 TT runner.timer、scheduler.timer、runner.path 停止；旧源码不认识历史执行门禁，不能自动恢复触发器。回滚不恢复旧 SQLite。实际备份及验证结果由生产部署回执记录。


## 生产部署回执（2026-10-16，北京时间）

- CPU `43.166.187.96`：TT源码提交 `06090367b30d867fc5ed3ae2e2f5045ca203bff3`；FB客户端 `41b5ca4f1e8f64945f56b2482e8e3dd96e2c9e89`；日报 `e2da31a69e23a333ba9e92b970a99476ec5d9d68`。均已推送GitHub，CPU按精确提交拉取并生成保留原运行目录其他文件的独立release。
- CPU备份 `/mnt/data-disk/post-repair-20261016/backups/20261016T032749Z/cpu`；TT、FB、日报发送账本在线备份均通过quick_check。香港GPU配置备份 `/data/post-repair-20261016/backups/20261016T032749Z`。
- 生产回归278项：TT102、FB92、日报84，全部通过。GPU真实来源分段读取HTTP206、MP4头有效；TT/FB纯校验接受业务CDN并拒绝未授权域名，无测试发帖或测试制作任务。
- TT历史执行门禁运行值 `2026-10-15T16:00:00+00:00`。TT/FB历史任务发布尝试次数均零增加；原日报、原发送回执、delivery.sqlite3逻辑记录保持不变。所有原先启用的触发器已恢复。
- 11:34后TT自动任务3389/3390/3391（北京时间16日05:17计划）正常进入制作，其来源均为业务CDN。11:36快照仍在制作中；不将制作开始记为发布成功。任务3221在约两分钟观察窗只领取核对1次，next_attempt安排11:39:05；线上health 200、automation.ready=true。
- 历史任务3323通过核对已有publish_id确认成功，其publish_attempt_count仍为1；这是既有结果确认，不是新增发布。
- 日报当前状态验证独立保存于 `/mnt/data-disk/post-repair-20261016/validation/`，未发送、更改或覆盖10:00原日报。中午重读任务状态可能包含10:00后的变化，该预览明确标记为当前状态，不能替代10:00快照。

代码回滚（CPU）：

```sh
python3 /mnt/data-disk/post-repair-20261016/cpu_deploy.py rollback /mnt/data-disk/post-repair-20261016/stage.json
```

GPU来源配置回滚（按需）：

```sh
/opt/fb-page-random-overlay/venv/bin/python /data/post-repair-20261016/gpu_source_hosts.py rollback 20261016T032749Z
```

回滚不恢复任何SQLite，不清理发布账本。CPU回滚会保持TT/FB生产触发器停止；旧TT代码不识别历史门禁，必须保留停用状态，直到重新具备该保护后才恢复。
