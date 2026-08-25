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
6. 原子切换 `/opt/x-post-automation/current`；同步共享 `features/x_posts/service.py`、静态页
   及 Nginx 公网页副本。主 API 的 `app.py` 必须从其线上精确基线单独构建，禁止用 sidecar
   release 中的旧基线覆盖。
7. 仅重启 `x-post-automation.service`、`drama-material-api.service`，恢复原 timer 状态；
   不手工启动发布 oneshot。

## 验证步骤

- 两个服务 active、sidecar loopback health 正常、主 API 匿名权限门返回 401，current symlink
  指向精确 commit。
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

### 版本与回滚点

- 执行时间：2026-08-25 16:07-16:23（北京时间）。
- sidecar/共享 service/页面提交：
  `960816e64e9d889d99fad313466a655316692ed6`，远端 commit 已验证可读取。
- 主 API 线上 `app.py` 实际基线对应提交：
  `f9b358e1e1493b2a5aff2817cae9c8408387559d`。为避免覆盖该基线之后的账号统计功能，
  仅把 deferred 合同移植为 app-only 提交
  `6f8bdf0a02636377150d1def0fa91213da07a52f`，远端 commit 已验证可读取。
- 新 immutable release：
  `/mnt/data-disk/x-post-automation/releases/960816e64e9d889d99fad313466a655316692ed6`。
- 部署前 release/回滚点：
  `/mnt/data-disk/x-post-automation/releases/3b9cda698e8f4ad1a025a8f9e2e1dd6296c95769`。
- 权限 `0700` 的部署前备份：
  `/mnt/data-disk/x-post-automation/backups/20260825T160936+0800-x-deferred-deliverable-pre-960816e`。
- SQLite online backup SHA-256：
  `b2147254b10e65bc610abad58b03621c37eb10a1729cc46256bd8da24c8349cb`；
  `quick_check=ok`、foreign key error=0。

### 部署哈希

| 文件 | SHA-256 |
| --- | --- |
| current/main API `features/x_posts/service.py` | `cb9ff9fa11781ce4f3c22c7cccca6e3e6c4c4175c03dfa4d30b0fe6c24d13943` |
| 主 API `app.py` | `a956fb9952aa09d8d911cf3a5c54b58525cb81935d92d0ede698af9c681675a3` |
| 主 API/Nginx 公网页 `x-post-material-pool.html` | `56201c659abf91fb20d42f1798e8b618ae83e804ebe6ff051f9eff5eb9371391` |

公网 HTTPS 获取到的页面 hash 与 Nginx 文件一致。页面已确认包含“待可投放”、
“下一次自然排期自动复检”和 `drama_not_yet_deliverable` 三项标记。

### 健康、台账与自然 timer 验收

- `x-post-automation.service`、`drama-material-api.service` 均为
  `active/running`、`Result=success`、`NRestarts=0`；sidecar `/health=ok`，主 API 匿名
  material-pool 请求返回预期 401。服务重启后的第一次即时主 API curl 早于监听完成而
  connection refused；随后端口、权限门和服务状态全部通过，属于启动窗口而非服务失败。
- 部署前暂停三个会生成发布工作的 timer；部署后仅恢复 timer，未手工启动任何发布
  oneshot。`x-post-schedule`、`x-post-schedule-claim`、`x-post-manual` 各自然运行一轮，
  均 `Result=success/ExecMainStatus=0`；三个 timer 最终均为 `active`。
- 部署前后 `x_post_queue=627`、`x_post_publish_log=627`、`unknown_outcome=0`；目标池
  843/844/845/847/848 的关联 queue 数始终为 0。没有为验收创建真实 X Post。
- 在生产 SQLite online backup 的临时副本上加载新 release：5 条目标记录均返回
  `availability=deferred`，`summary.deferred=5`，且 5 条全部重新进入
  `available_pool_items()` 候选；临时副本目标 queue=0，用后删除，生产库未写。
- 目标行仍保留原 `drama_not_yet_deliverable` 和旧检查时间是预期行为：只有下一次自然
  素材槽真正选择并完成当前预检时，才允许在建队列事务中清除旧错误；部署不伪造复检。
