# 013.x-post-media-repair 开发计划

1. 在 `features/x_posts/media_repair.py` 实现安全下载、源指纹确认、画布选择、NVENC 转码、正式 probe、COS 发布、manifest 和幂等锁。
2. 在 `scripts/x_post_media_repair_worker.py` 提供 loopback 健康检查和 Bearer POST 接口。
3. 增加 GPU worker/tunnel systemd unit 与无密钥 env 示例。
4. 在 `scripts/x_post_daily_runner.py` 增加严格 repair client、配置门禁和三类错误的一次性修复/二次预检；超长固定裁尾至 139 秒。
5. 在 `features/x_posts/service.py` 增加 queue 审计字段、幂等迁移和严格字段组合校验。
6. 增加 worker、daily、ledger 单测以及全套 X 回归。
7. GitHub-first 提交并推送；CPU/GPU 分别从精确 commit 构建 release，先备份后切换。
8. 先部署 worker 和隧道，验证 health、鉴权和 GPU/COS 单条 canary；再部署 CPU runner/schema。
9. 对当前九条执行只修复/复检 backfill，确认零 queue/log/Post；保持今日账号不再发布。
10. 复核 timer、Sidecar、SQLite、COS URL、重复计数和回滚点。

## 2026-07-29 增量

11. profile/job/COS/manifest 升级为 v2，补超长/过短/边界/复合首错回归。
12. 增加短剧精确成功重验门禁及 `x_post_drama_media_repair_backfill.py`，全链成功后才清错，工具不具备建计划或发布能力。
13. GPU、CPU GitHub-first 部署并暂停 timer 消除 profile 切换窗口；精确修复池 53/54 后只恢复自然调度。
