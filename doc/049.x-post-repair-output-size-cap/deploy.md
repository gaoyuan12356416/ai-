# 部署与回滚

## 部署范围

- CPU：`features/x_posts/media_repair.py`、依赖该 profile 的调度/手动/X Auto 运行时与环境配置。
- GPU：`features/x_posts/media_repair.py`、`scripts/x_post_media_repair_worker.py` 和修复服务 profile。
- 数据库迁移：无。

## 安全边界

- 部署前暂停五个 X 自动触发 timer，等待在途 runner 和 GPU 修复结束。
- 对 CPU/GPU 当前 release、unit、环境文件和 SQLite 做备份；Token 只记录哈希、权限和属主。
- 先部署并验证 GPU，再部署 CPU；仅重启受影响服务。
- 不恢复旧 SQLite 或 Token，不补发历史失败批次，不执行 run-now/canary/manual 发布。

## 回滚

1. 暂停相同 timer 并等待在途工作退出。
2. 将 GPU 和 CPU `current` 原子切回各自部署前 release。
3. 恢复备份的非秘密环境文件和 unit，执行 `daemon-reload`。
4. 重启 GPU、Sidecar 与 X Auto，验证反向隧道、健康接口和 profile。
5. 恢复原 timer 状态；保留当前 SQLite、Token、队列和发布账本。

