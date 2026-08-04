# 部署文档

## 变更内容

- TT 个号管理增加剧语言设置。
- 自动调度按账号当前语言跨全池领取同语言 FIFO。
- 预制作表增加剧语言和实际领取账号语义。
- 账号设置表增加默认 `en` 的持久字段。
- 不修改 GPU worker、TikTok Direct Post 协议、短链协议或真实 Token。

## 配置项

无新增环境变量。继续使用：

- 数据库：`/mnt/data-disk/tt-post-publisher/tt-post.sqlite3`
- 发布目录：`/opt/tt-post/releases/<commit>`
- 当前链接：`/opt/tt-post/current`
- Sidecar：`tt-post-service`
- Runner：`tt-post-runner.timer`
- Prepare：`tt-post-prepare.timer`

## 数据库变更

Sidecar 启动时幂等执行：

```sql
ALTER TABLE tt_post_account_setting
ADD COLUMN drama_language TEXT NOT NULL DEFAULT 'en';

ALTER TABLE tt_post_recurring_pool
ADD COLUMN routing_language TEXT NOT NULL DEFAULT '';
```

仅当列不存在时执行。随后按服务端统一规范函数回填 `routing_language`：历史空语言回填 `en`，非法历史值写隔离键；再校验或重建 `(status,routing_language,created_at,id)` 复合索引。`tt_post_schedule_run` 和 `tt_post_queue` 不新增列。

上线前必须：

1. 对生产 SQLite 使用在线 backup API 备份，不直接复制活动 WAL 文件。
2. 在副本连续初始化两次。
3. 检查 `PRAGMA integrity_check`、账号默认值、素材路由键/索引、历史 queue/run/publish_id 数量和素材状态分布不变。
4. 审计历史空/非空素材语言分布；空值按 en 是已确认语义。

## 部署步骤

1. GitHub-first：确认目标 commit 已推送，工作树干净，记录上一个 release commit。
2. 选择没有自动到期槽的安全窗口；不为部署测试手工触发 run-now 或 schedule due。
3. 创建备份目录：

   ```text
   /mnt/data-disk/tt-post-publisher/backups/<timestamp>-language-routing-pre-<short-commit>
   ```

4. 备份 SQLite、`/opt/tt-post/current` 指向和三份相关静态页。
5. 从 GitHub 精确 commit 构建只读 release，先在隔离数据库运行 py_compile、全量 TT 测试和迁移副本检查。
6. 在无到期槽窗口先停止 runner/prepare 的 timer、path 和 `tt-post-service`，并确认旧 writer 已退出；不停止 GPU/Nginx。
7. 原子切换 `/opt/tt-post/current`。
8. 安装同 commit 的 `tt-account-settings.html`、`tt-post-pool.html` 到主后台静态目录和 Nginx 公共目录。
9. 启动 `tt-post-service` 完成增量迁移，再恢复 runner/prepare timer 和 path；不手工执行真实发布。
10. 确认 sidecar、runner/prepare timer/path 和 Nginx 均为 active；不改变自动配置开关、账号成员或发布时间。

## 验证步骤

### 服务与文件

- `systemctl is-active tt-post-service tt-post-runner.timer tt-post-prepare.timer tt-post-runner.path tt-post-prepare.path nginx`
- Sidecar health 正常，journal 无迁移异常或 Token 文本。
- 三份静态页 SHA-256 与 GitHub blob 一致。

### 数据库只读

```sql
PRAGMA integrity_check;
PRAGMA table_info(tt_post_account_setting);
PRAGMA table_info(tt_post_recurring_pool);
PRAGMA index_info(idx_tt_post_recurring_pool_language_fifo);
SELECT drama_language, COUNT(*)
FROM tt_post_account_setting
GROUP BY drama_language;
SELECT status, COUNT(*) FROM tt_post_recurring_pool GROUP BY status;
SELECT status, COUNT(*) FROM tt_post_queue GROUP BY status;
SELECT COUNT(*) FROM tt_post_queue WHERE publish_id<>'';
```

- 新列存在且无 NULL/空账号语言。
- 部署前后 intake/pool/queue/run 行数和终态数量一致。
- 不通过保存接口写生产账号语言作为部署探测。

### 页面与 API

- 登录态只读打开个号管理，确认剧语言默认/回填可见；不点击保存。
- 打开 TT Post 发布池，确认后台预制作表有“剧语言”，未领取行显示等待文案。
- GET 账号/素材池接口不返回凭证；`account_settings.drama_language` 和 `material_language` 存在。
- 100% 缩放下页面无重叠或横向溢出。

### 发布安全

- 不调用 `run-now`、不手工执行 schedule due、不创建真实 TikTok Post。
- 自动化验收只用临时数据库和 fake TikTok/GPU。
- 若部署窗口自然出现已有生产排期，只做账本只读核对，不把自然业务发布冒充为本需求 canary。

## 回滚方案

1. 停止进一步发布变更，记录当前 queue/run/publish_id；未知结果不得补发。
2. 将 `/opt/tt-post/current` 原子切回上一个已验证 release，并恢复对应静态页。
3. 在旧 writer 全部停止后切回 release，启动 `tt-post-service` 并恢复 timers，确认 Nginx 状态。
4. 保留新增 `drama_language`、`routing_language` 列和索引；旧代码会忽略 additive 字段，不应回滚整个活动数据库。
5. 仅在完整性损坏且确认没有新 queue/publish_id 时，才由人工评审离线恢复数据库备份。不得用旧备份覆盖已产生的发布身份。

回滚后旧代码不再提供语言路由保护；若原因涉及跨语言风险，应由运营显式暂停自动配置，完成修复和数据审计后再恢复。

## 注意事项

- 自动领取会把 pool 的 `account_id` 和 `is_aigc` 改为实际领取账号，这是预期账本变化。
- 已存在 run/queue/publish_id 不因配置后改重选。
- 手动立即发布仍按精确账号分池，不能用它验证自动语言路由。
- 新代码迁移前必须停净旧 writer；禁止一边让旧进程新增空 `routing_language` 行，一边启动新版本。
- 技能上下文若无新的长期运维规则可保持不变；部署记录和实际 commit/备份路径必须回填本文件。

## 2026-08-04 生产发布记录

- GitHub/生产 release 提交：`af95ea73d95b883e591318c7e0ab09cfeb4716e4`。
- 前一 release：`/opt/tt-post/releases/aadbd95358cca632bb3e8764d633c2e1732d165b`。
- 当前 release：`/opt/tt-post/releases/af95ea73d95b883e591318c7e0ab09cfeb4716e4`；sidecar 于 17:57:12 CST 启动，cwd 与该 release 一致。
- Online backup：`/mnt/data-disk/tt-post-publisher/backups/20260804T175523+0800-language-routing-pre-af95ea7`；原始副本 SHA-256 为 `30df41c4860bee8543297673e810c97257efae78593a69445014942e75fbef5b`，`integrity_check=ok`。
- 候选机使用实际 Python/SQLite 环境通过完整 TT Python 372/372、Drama bridge 53 项和编译。首次候选仅因旧 SQLite 不支持测试用 `DROP COLUMN` 而失败，未切换线上；兼容修复后复验通过，见 `bugs/BUG-002.md`。
- 数据库副本连续初始化两次，迁移前后业务基线逐行一致；生产迁移后 19 个账号语言均为 `en`，25 条 recurring 素材路由键均为 `en`，语言 FIFO 查询命中 `idx_tt_post_recurring_pool_language_fifo`。
- 部署前后保持：intake 24、recurring 25（available 17/canceled 1/consumed 7）、queue 7、run 7、非空 `publish_id` 6、活动 queue/run 0。
- 页面 SHA-256：个号管理 `d5f7ae0c2ce67ef5c678dd207355756b9a5cf32de237836fd459281738082efc`；发布池 `7756cdeb75c2d8a7bc256172da7ee6cbca0cd84e222de8adec2dfe307e950778`。release、应用 static、Nginx static 和公网无缓存内容一致。
- Chrome 登录态 100% 缩放验收：viewport 2560×1215、DPR 1、页面 `scrollWidth=clientWidth=2545`；个号语言输入回填 `en`，预制作表有“剧语言”列和“等待同剧语言账号领取”，随机配置区无重叠。
- `tt-post-service`、runner/prepare timer、runner/prepare path 与 Nginx 均为 active；18:00 自然 runner 为 `schedule_due_count=0`、`publish_request_count=0`、`status=ok`，prepare 为 idle。
- 验收未保存账号、未加入素材、未手工触发 due/run-now、未创建真实 TikTok Post。

### 精确回滚点

1. 停止 runner/prepare timer、path 和 `tt-post-service`，确认无活动 writer。
2. 将 `/opt/tt-post/current` 原子切回 `/opt/tt-post/releases/aadbd95358cca632bb3e8764d633c2e1732d165b`。
3. 从上述 backup 的四份 `app/nginx-tt-*.html` 恢复两个页面，启动 sidecar、timer 和 path。
4. 保留 additive 语言列和索引；除非数据库本身损坏且确认没有新发布身份，否则不得用备份覆盖活动库。
