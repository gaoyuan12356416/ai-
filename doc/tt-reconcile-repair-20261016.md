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
