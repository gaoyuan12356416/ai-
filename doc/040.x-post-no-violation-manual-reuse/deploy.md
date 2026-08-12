# 040 部署与回滚

## 部署

1. 从当前生产 release 的直接后继分支构建、测试、提交并推送 GitHub。
2. 记录 Sidecar/main runtime 精确文件 hash、release、服务/timer、queue/log/Post/unknown/active 基线。
3. 验证 `/mnt/data-disk` 挂载、空间和可写性；创建 SQLite 在线备份、运行文件、unit、配置与 token 非敏感 owner/mode/hash 清单。
4. 在生产 SQLite 备份副本连续执行两次 `ensure_storage()`，验证合法手动重复状态和自动去重触发器。
5. 从精确 GitHub commit 构建不可变 release，短暂停止相关 timer/oneshot，原子切换 Sidecar，并同步主 API 的 `service.py`/`selector.py`。
6. 只重启 `x-post-automation.service` 与 `drama-material-api.service`，恢复原 timer 状态。
7. 验证 health、schema、`integrity_check`、自然 `no_pending/no_due` 和账本基线不变；禁止真实发帖 canary。

## 回滚

- 若新策略尚未产生重复手动 `material_key`，可切回前一 release、恢复主 API 文件并重启服务；旧 release 可重建全局唯一索引。
- 若已产生合法的重复手动历史，不得删除 queue/log 或恢复旧 SQLite。旧 release 无法安全重建全局唯一索引；应暂停新的手动入口并采用向前修复或兼容回滚 release。
- 无论何种回滚，都不得恢复旧 token 内容、重放队列或手动触发 X Post。
