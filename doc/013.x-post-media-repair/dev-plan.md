# 013.x-post-media-repair 开发计划

1. 在 `features/x_posts/media_repair.py` 实现安全下载、源指纹确认、画布选择、NVENC 转码、正式 probe、COS 发布、manifest 和幂等锁。
2. 在 `scripts/x_post_media_repair_worker.py` 提供 loopback 健康检查和 Bearer POST 接口。
3. 增加 GPU worker/tunnel systemd unit 与无密钥 env 示例。
4. 在 `scripts/x_post_daily_runner.py` 增加严格 repair client、配置门禁和两类错误的一次性修复/二次预检。
5. 在 `features/x_posts/service.py` 增加 queue 审计字段、幂等迁移和严格字段组合校验。
6. 增加 worker、daily、ledger 单测以及全套 X 回归。
7. GitHub-first 提交并推送；CPU/GPU 分别从精确 commit 构建 release，先备份后切换。
8. 先部署 worker 和隧道，验证 health、鉴权和 GPU/COS 单条 canary；再部署 CPU runner/schema。
9. 对当前九条执行只修复/复检 backfill，确认零 queue/log/Post；保持今日账号不再发布。
10. 复核 timer、Sidecar、SQLite、COS URL、重复计数和回滚点。
