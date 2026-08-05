# SA 代码评审

## 状态

核心架构评审、真实浏览器无发布验收和最终安全复核均已完成；生产部署与启用仍需单独授权。

## 评审结论

- 独立系统边界成立；旧 `features/tt_posts`、旧发布池页面和账号设置页面无 diff。
- MySQL 指向固定只读副本；旧 SQLite 使用 `mode=ro` 与 `query_only`；查询参数化且按日流式聚合。
- 正式选择在黑名单/旧历史最终复核后原子冻结，预览不冻结；失败和回滚均不释放素材账本。
- 自动 run 建立时在事务内复核当前启用状态、版本与 `enabled_at_utc`，关闭停用/编辑竞态和启用前补跑窗口。
- 手动立即执行使用客户端持久化幂等键；未知/5xx 重试复用，确定成功或确定 4xx 后清理。
- 调度器只负责每分钟 `tick`，worker 独立执行耗时任务；账号串行、手动优先与 lease fencing 由独立账本保证。
- 发布状态机冻结素材、GPU job ID 和短链事实；存在 `publish_id` 或 unknown outcome 后只能 reconcile，不能重新 initialize/publish。
- 三个独立发布门禁默认关闭，且新 bearer 不得复用 GPU 或旧 TT 内部 token。
- 文档示例 bearer 会被主 API、sidecar 和 runner 明确拒绝；sidecar 关闭会等待在途发布请求结束。
- 浏览器 DTO 删除源素材/准备素材 URL 和黑名单值明细；错误信息脱敏，发布 URL 仅允许受信的 TikTok HTTPS URL。
- 北京时间计划、完整业务日和 Decimal 文本存储/比较无隐式浮点误差；指标 generation 有界保留。

## 生产启用前剩余门禁

- 部署精确 GitHub 提交并执行生产“关闭默认”验收；部署前复跑 108 个新系统测试、64 个旧 TT 回归、JS 语法和 `git diff --check`。
- 部署时保持三重门禁关闭、模板库为空；不得用真实 TT 帖子作为验收手段。
