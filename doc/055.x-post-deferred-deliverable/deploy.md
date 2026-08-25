# 部署文档

## 变更内容

- 素材池派生 `deferred` 状态、候选复检和 FIFO 时间证据门禁。
- 主 API 新增 `availability=deferred`、`summary.deferred`、`deferred_count`。
- 素材池页面新增“待可投放”统计、筛选和中文说明。
- 无 schema、Token、账号、模板、发布时点或历史 queue/log 重写。

## 配置项

无新增环境变量。复用现有只读 MySQL、SQLite、素材排期 timer 和 X sidecar 配置。

## 数据库变更

无 DDL/迁移/回填。上线前仍使用 SQLite online backup 并执行 `quick_check`；回滚时保留
当前台账，不用旧数据库覆盖上线后的自然业务事实。

## 部署步骤

1. 提交并推送精确 GitHub commit，确认远端对象可读取。
2. 记录 current release、mount UUID/空间、相关 service/timer、queue/log/unknown 和 5 条
   目标池记录基线。
3. 暂停 schedule/claim/manual 等会创建 X 发布工作的 timer；等待已运行 oneshot 排空。
4. 创建权限 `0700` 的备份目录：SQLite online backup、当前 release/主 API/公网静态页、
   unit/timer/symlink/hash manifest；Token 只记录路径权限和 SHA，不输出内容。
5. 从 GitHub 精确 commit 建 immutable release，在 release 内执行 py_compile 和专项测试。
6. 原子切换 `/opt/x-post-automation/current`；同步主 API 的 `app.py`、共享
   `features/x_posts/service.py`、静态页及 Nginx 公网页副本。
7. 仅重启 `x-post-automation.service`、`drama-material-api.service`，恢复原 timer 状态；
   不手工启动发布 oneshot。

## 验证步骤

- 两个服务 active、loopback/主 API health 正常，current symlink 指向精确 commit。
- release、主 API、app/static 与 Nginx 公网页 hash 一致。
- SQLite `quick_check=ok`、foreign key error=0。
- 在 SQLite online backup/临时副本验证 5 条历史行为 `deferred` 且进入候选查询；不写生产库。
- 匿名公网请求仍被权限门禁拒绝；页面包含“待可投放”和自动复检说明。
- 恢复 timer 后只观察自然 `no_due`/零 claim 或正常业务 run；对比 queue/log/unknown，
  不把自然业务发布算作部署测试。

## 回滚方案

1. 再次暂停相关 timer，等待 oneshot 退出。
2. 原子切回部署前 release；恢复备份的主 API/共享 service/静态页文件。
3. 重启两个受影响 service 并恢复 timer。
4. 保留当前 SQLite、Token、queue/log/Post 事实；仅当数据库自身损坏且已单独批准时才使用
   online backup。

## 注意事项

- `features/x_posts/service.py` 同时被 release sidecar 与主 API 加载，必须双副本一致。
- `/usr/share/nginx/html/x-post-material-pool.html` 是独立公网页副本，也必须同步。
- 5 条历史素材已经过原可投放时间；上线后的下一次自然素材槽可能合法选中其中一条。
- 截图中的 X 403 临时锁号是独立账号故障，本部署不重试该失败 queue。

## 生产执行记录

待部署后回填精确 commit、备份路径、回滚 release、hash、timer 和台账计数。
