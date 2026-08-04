# 部署与回滚

## 部署前

1. 记录生产 release、服务/定时器状态及 TT 数据库路径。
2. 使用 SQLite online backup 备份数据库并执行 `PRAGMA integrity_check`。
3. 导出表/列清单和自动配置、排期、日计划、运行、队列的非敏感计数。

## 发布

1. 本地测试通过后提交并推送 GitHub。
2. 服务端从精确提交构建不可变 release。
3. 在数据库副本演练迁移；成功后切换 release。
4. 只重启 `tt-post-service`；保持 GPU 服务不变。
5. 不手动启动发布 runner，不创建真实任务。

## 验证

- health、systemd service/timer、页面/API、SQLite schema/integrity。
- 用隔离数据库生成固定及随机计划，验证 60 分钟、非整点、账号隔离和重启稳定。
- 对比生产发布 ID、队列和运行基线；部署验证期间不得新增真实发布请求。

## 回滚

- 将 `/opt/tt-post/current` 切回部署前 release，恢复静态页并重启 `tt-post-service`。
- 新表和加法字段可保留；如必须恢复数据库，先停服务后使用部署前 online backup。

## 2026-08-04 生产发布记录

- GitHub/生产提交：`139a7c477f7336a10a9daece950c397d18f5a4e5`
- 前一 release：`/opt/tt-post/releases/e11305771246dea484f3a11c5a62dfc46a60b9fb`
- 当前 release：`/opt/tt-post/releases/139a7c477f7336a10a9daece950c397d18f5a4e5`
- online backup：`/mnt/data-disk/tt-post-publisher/backups/20260804T151255+0800-multi-random-pre-139a7c4`
- 数据盘 UUID：`3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`
- 副本迁移：`integrity_check=ok`，历史配置保持版本 6、启用、7 个账号、固定 `14:00`；新随机计划表为空。
- 首次切换因新 release 根目录继承 `0700` 权限，`tt-post` 用户无法读取而失败；自动回滚到前一 release，数据库未迁移，队列/运行/发布 ID 均未变化。按现有不可变 release 模型修正为目录 `0555`、普通文件 `0444`、可执行文件 `0555`，并以 `tt-post` 用户导入通过后重新发布。
- 最终切换：2026-08-04 15:16:50 CST；`tt-post-service` 健康，runner/prepare 的 timer 与 path 均 active。
- 三处静态页（release、应用 static、Nginx static）SHA-256 均为 `4c3919ce3094c35cd789eebd3974a6a0ea7ddf965945101f4c984a43a663dfda`，公网无缓存获取结果一致。
- 部署前后生产基线均为：队列 7、最大队列 ID 7、非空 TikTok `publish_id` 6、运行 7、最大运行 ID 7。
- 15:17 CST 自然 runner：`status=ok`、`schedule_due_count=0`、`publish_request_count=0`；未人工触发 runner 或真实发布。

### 精确回滚

1. 将 `/opt/tt-post/current` 原子切回 `/opt/tt-post/releases/e11305771246dea484f3a11c5a62dfc46a60b9fb`。
2. 从上述 backup 的 `static/app-tt-post-pool.html` 与 `static/nginx-tt-post-pool.html` 恢复两处页面。
3. 只重启 `tt-post-service.service`；加法字段和空计划表可安全保留。
4. 只有数据库本身损坏时，停服务后才使用 `tt-post.sqlite3.pre`；该 pristine 副本 `integrity_check=ok` 且未执行新迁移。

## 2026-08-04 UI 布局修复发布记录

- GitHub/生产提交：`aadbd95358cca632bb3e8764d633c2e1732d165b`
- 前一 release：`/opt/tt-post/releases/139a7c477f7336a10a9daece950c397d18f5a4e5`
- 当前 release：`/opt/tt-post/releases/aadbd95358cca632bb3e8764d633c2e1732d165b`
- online backup：`/mnt/data-disk/tt-post-publisher/backups/20260804T164744+0800-random-layout-pre-aadbd95`
- 运行时代码、unit、API 和数据库 schema 均未修改；相对前一运行提交仅更新页面 CSS、页面契约测试和 requirement 文档。
- 本地完整回归：358 个 Python 测试、TT bridge 53 项断言、编译和 `git diff --check` 均通过；候选 release 页面测试 36/36 通过。
- 2026-08-04 16:48:53 CST 完成原子切换；只重启 `tt-post-service` 以使进程 cwd 对齐新 release，Nginx、GPU、runner/prepare timer 与 path 均未重启或手工触发。
- sidecar MainPID 从 `1869843` 更新为 `1922749`，cwd 指向新 release；`127.0.0.1:18829/health` 返回 `ok=true`，service、两个 timer、两个 path 与 Nginx 均为 active。
- release、应用 static、Nginx static 和公网无缓存页面 SHA-256 均为 `774dc80027a1322e9eec584946624805dbb1c1323d43cfcd0b55a4d15f04217c`。
- 切换前后数据库均为 `integrity_check=ok`，基线保持：队列 `7`、最大队列 ID `7`、非空 `publish_id` `6`；运行 `7`、最大运行 ID `7`；随机日计划 `7`。
- 16:49 CST 自然 runner 返回 `status=ok`、`schedule_due_count=0`、`publish_request_count=0`；未人工触发 runner 或真实发布。
- 生产静态页与已在 100% 缩放下验证的 GitHub blob 完全同 SHA；2560 至 820px 多宽度无横向溢出或保存按钮/账号设置重叠。

### UI 修复精确回滚

1. 将 `/opt/tt-post/current` 原子切回 `/opt/tt-post/releases/139a7c477f7336a10a9daece950c397d18f5a4e5`。
2. 从上述 backup 的 `static/app-tt-post-pool.html` 与 `static/nginx-tt-post-pool.html` 恢复两处外部页面。
3. 只重启 `tt-post-service.service`；Nginx 配置未变化，无需 reload。
4. 本次没有数据迁移；只有数据库本身损坏时，停服务后才使用 backup 中 `tt-post.sqlite3.pre`。
