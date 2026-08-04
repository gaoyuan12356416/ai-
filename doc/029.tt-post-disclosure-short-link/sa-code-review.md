# SA 代码评审

## 结论

通过。变更只影响 TT 自动队列及其短链命名；立即测试任务、历史任务和 X 短链保持原合同。

## 关键检查

- 自动任务在策略生成、队列持久化和最终 `begin_publish` 三处均强制写入 `brand_content_toggle=false`、`brand_organic_toggle=false`。即使部署前旧任务仍保存为 `true`，越过发布网络边界前也会清零。
- `BEGIN IMMEDIATE` 事务内读取 `sqlite_sequence`，以即将分配的 `tt_post_queue.id` 生成短链；插入后校验 `lastrowid`，不一致则整个事务回滚。
- 新自动链接为 `/s2l/tt/{queue_id}.html`；旧 `8xxxxxxxxxxxxxxxxxx` TT 链接继续在旧目录读取，立即测试仍使用原独立命名空间。
- Nginx 精确匹配 `/s2l/tt/` 后再进入 X 的纯数字路由，避免 `/s2l/6.html` 与现有 X 链接冲突。
- 短链文件仍采用原子、不可变写入；同 ID 不同目标失败关闭，既有文件不会被覆盖。
- 无数据库 schema 或历史数据批量修改，无 GPU 代码和发布协议变更。

## 风险与控制

- SQLite 自增序列极限：沿用 SQLite 正整数上限，并在业务层校验。
- 旧幂等请求：按数据库已冻结的旧短链重新渲染并校验，不会被新命名规则误判为冲突。
- 回滚：代码 release 与 Nginx snippet 可独立回退；新路径文件不影响旧代码与 X 路由。
