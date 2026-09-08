# X 发布前缺口修复

## 行为

- 混合语言素材批次只选到部分账号时，FIFO 校验继续验证最后一个选中素材之后的语言容量证明。证明必须存在于当前可用池，来源身份、语言、校验时间和已用容量仍须一致；未知、过期或不匹配的证明使事务回滚。
- 剧集预检在完整校验当前源数据后比较免费集数。范围变化先走内部 `POST /internal/posts/drama-pool/sync-progress` 同步，再重新获取当前账号的可用剧集。已经播完最新免费范围的剧变为 `completed`，同一批次可接续同语言的新剧。
- 同步需要原始池快照（免费集数、下一集、已发布数、绑定账号、重播代次）。事务检查每一集的已确认发布账本及转发状态。待发布、失败、未知结果、进度不一致或并发快照变化都阻止同步。保留队列、日志、Post ID、实际已发布数和原绑定证据；免费范围缩小时不会抹掉历史集数。
- `x_post_drama_progress_sync_audit` 记录范围变更及前后状态。`validate_only=true` 只验证；成功请求重放为无副作用读取。
- X Auto 将绝对 HTTP 素材地址仅在内存中升级到 HTTPS，继续执行地址、媒体、身份、时长、ROAS 和去重校验，不回写源表。
- 无候选任务的事件保存 `details.rejection_counts`，错误说明显示各类淘汰数量。现有模板阈值及历史任务保持原状。

## 验证

离线回归覆盖尾部容量证明、过期/伪造证明、免费范围缩小及增加、低于已发布进度的范围缩小、并发快照冲突、未完成队列保护、同步后同账号接续新剧、HTTP 地址转换后的身份/端口保护，以及无候选事件记录且不创建发布。

```text
python -m unittest scripts.test_x_post_multi_schedule_store scripts.test_x_post_drama_selector scripts.test_x_post_schedule_runner scripts.test_x_auto_post_publisher scripts.test_x_auto_post_store scripts.test_x_auto_post_selector scripts.test_x_posts scripts.test_x_post_auto_template_bridge -q
```

## 部署和回滚

1. 在 GitHub 发布完整生产基线及修复提交，服务器取得该精确提交并运行 Linux 测试。
2. 停止 X 发布触发器，等待正在执行的服务自然结束，取得共享发布锁。
3. 在线备份两个 SQLite 账本、令牌目录、主 API 待改文件，保存服务状态及文件校验值。新表迁移先在备份副本演练。
4. 切换 X 不可变发行版，并同步主 API 的 `features/x_posts/service.py`、`drama_selector.py`；只重启 X Sidecar、X Auto 服务和主 API。
5. 检查健康、数据库完整性、迁移幂等性、确认发布记录不变，恢复原触发器。只读源审计后的进度同步不创建 Post。
6. 回滚时再次停止触发器并等待发布者结束，切回保存的旧发行版，恢复对应主 API 文件并重启上述服务，恢复原触发器。保留最新 SQLite 和令牌状态；新审计表向后兼容，不用旧备份覆盖新的发布事实。

生产账号、具体队列、恢复范围和备份路径仅写入私有运维报告。
