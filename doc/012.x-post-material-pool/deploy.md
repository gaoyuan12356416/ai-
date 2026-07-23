# 012.x-post-material-pool 部署文档

## 当前状态

“入池即时 X 校验 + `ads_custom_source.url` 直链预览”已于 2026-07-23 按 GitHub-first 部署生产，精确运行 commit 为 `00b5b088af76dce4a02866beaf0186713daa46fb`。服务器 release clean，主后台、Sidecar 和素材池公网页面均与该 release 的目标文件哈希一致；Sidecar、主后台、`x-post-daily.timer` 为 active，daily service 为 inactive，下一次自然触发为 2026-07-24 10:00 CST。本次未启动 daily、未创建 queue/日志/短链、未上传媒体、未调用真实 X。

## 生产执行记录

- 生产只读 MySQL 会话确认 `ads_custom_source.product` 字段存在，既有 canary 素材 `5221348` 满足 Dramawave/type/delete 门禁；会话 `transaction_read_only=1`。
- 部署前备份位于 `/mnt/data-disk/x-post-automation/backups/20260723T082250Z-material-pool-75f46e7`，包含 SQLite、env、unit、主后台/公网静态基线与 release 证据；未输出秘密内容。
- SQLite 副本迁移与生产迁移均通过；迁移后账号/run/queue/log/pool 计数为 `10/0/1/1/0`，`PRAGMA integrity_check=ok`，原 canary queue/log 保留。
- 第一次维护窗在部署脚本写错预期索引名称时 fail closed 并自动回滚，服务、timer、旧 release 与原数据库全部恢复；修正校验名为 `ux_x_post_queue_pool_item_id` 后第二次部署成功。
- `/usr/share/nginx/html/quick-nav.js` 保持部署前 hash `64aa18e75b6f421cbb37f68150526bc576352d3c15c434fd06e526a3e0a6dccf`，只结构化更新公网 `navigation.json` 并部署素材池/日志页面。
- 初始上线时内部管理员查询与 daily available 均返回 200、池内 0 条；公网匿名管理 API 返回 401 且 `Cache-Control: no-store`，素材池页、日志页与 OAuth health 均返回 200。
- 初始上线时 Chrome 管理员登录态显示“池内素材/未发布/可供发布/已发布”均为 0，导航入口和筛选表格正常，浏览器 warning/error 为 0。
- 生产敏感配置值与部署前一致；daily env 权限为 0400、Sidecar env/SQLite 为 0600。
- 素材预览增量备份为 `/mnt/data-disk/x-post-automation/backups/20260723T090326Z-material-preview-9711ef7`；备份 manifest 自校验全部通过，包含上线前主后台、服务/公网页面和 SQLite 在线备份。
- 精确 commit 在服务器 141/141 通过；主后台/Sidecar/timer 最终均 active，daily inactive，SQLite `integrity_check=ok`，pool/queue/log 保持 `2/1/1`。
- 公网匿名素材预览返回 401 + no-store；管理员浏览器显示独立“素材预览 / Post 预览”列。`5503209` 成功 302 到实际 HTTPS MP4，`11761405635` 因素材源记录或 URL 不可解析返回 404。
- 首次错误补全的 commit SHA 在 checkout 阶段即被 Git 拒绝，未进入备份/覆盖/重启；后续两次门禁分别因服务尚未监听的瞬时 502、以及本机直连 401 无 Nginx no-store 的过严断言自动恢复旧文件。修正为本机就绪 + 公网 no-store 分层验证后部署成功。
- 本次运行 commit 的 GitHub 长 SHA 首次被错误补全，远端分支等值门禁在创建 release/备份/覆盖前停止；从本地 `rev-parse` 与 GitHub `ls-remote` 双重取得正确 SHA 后重跑。
- 本次上线前备份为 `/mnt/data-disk/x-post-automation/backups/20260723T093550Z-pool-entry-validation-00b5b08`，包含 SQLite 在线备份、主后台/公网文件、env、旧 release、运行文件 hash/mode；manifest 全部通过，数据盘 UUID 为 `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`。
- 精确 release 在服务器完整回归 143/143；主后台因首次使用 selector，同步部署 `features/x_posts` 包，所有目标文件与 release 哈希一致。
- 上线后只对现有两个未占用池记录回填 selector 检查字段：池 ID 3 / 素材 `5503209`、池 ID 4 / 素材 `11761405635` 均为 `material_not_found_or_ineligible`，页面派生状态“不可用”。pool/queue/log 仍为 `2/1/1`，SQLite `integrity_check=ok`。
- 只读列表验证：`5503209` 虽不合规但成功附加 `ads_custom_source.url`，域名为 `advertising-1306474899.cos.ap-hongkong.myqcloud.com`；`11761405635` 无源记录，`material_preview_url` 为空并显示“无法预览”。
- 最终主后台/Sidecar/timer 为 active，daily inactive，Sidecar health `ok`，公网素材池页 200，匿名管理 API 401 + no-store，部署后两个服务无 warning 级日志。
- Chrome 中原管理员标签页登录态已过期，只显示登录按钮，因此未代替用户重新登录做表格视觉验收；同一生产环境下的管理员内部接口、源 URL 查询和公网页面哈希均已验证。

## 变更内容

- 增量部署 X Post 全局人工素材池、管理员 API/页面和导航。
- daily selector 从前日 spend 排名切换为素材池 FIFO。
- 增量迁移 queue 的 pool 关联、唯一索引和跨表触发器。
- 素材池明细新增独立素材预览列；管理员点击后由主后台安全跳转到池内素材对应的 HTTPS 源 URL。
- 本次增量：添加素材前复用 X selector 做只读即时校验，校验结果与池记录原子写入；失败/不存在立即显示“不可用”。
- 本次增量：素材池列表直接附加 `ads_custom_source.url` 的安全 HTTPS 地址，页面直接打开源素材，旧 302 接口仅保留兼容。
- 保留现有 X 日批次、W2A/短链、日志、账号、timer 和失败语义。

## 配置项

- 新增 `X_POST_DAILY_POOL_AVAILABLE_PATH=/internal/posts/material-pool/available`。
- 新增 `X_POST_DAILY_POOL_CHECK_PATH=/internal/posts/material-pool/check`。
- 既有 `/etc/x-post-automation.env`、`/etc/x-post-daily.env` 的 ownership/mode、backend/daily bearer 隔离、三个固定账号 ID 不变。
- `X_POST_DAILY_SCAN_LIMIT` 默认/生产建议 1000、允许 3 至 1000，用于读取最老原始池记录。
- `X_POST_DAILY_CANDIDATE_POOL_LIMIT` 默认 50、允许 3 至 100，用于保留合规候选供媒体补位，且不得大于 scan limit。
- 首次部署继续使用次日 `X_POST_DAILY_START_DATE`，防止 Persistent timer 当天补跑。

## 数据库变更

- 新表 `x_post_material_pool`。
- `x_post_queue` 新增 `pool_item_id`、`pool_created_at`。
- 新增 pool FIFO、queue pool ID 唯一索引。
- 新增 pool/queue 绑定一致性、池中素材不能被非池 queue 绕过、已占用池记录不能删除的触发器。
- 迁移函数为 additive/idempotent；legacy 重复 material 或账号日冲突继续 fail closed。

## 部署前门禁

1. CR-001 至 CR-004、CR-006 已关闭，最终离线回归 139/139 通过。
2. Dramawave product exact-match 在生产 schema 副本/只读查询中确认。
3. 最终工作树全部 X 测试、编译、JS 和 diff 检查通过。
4. GitHub 已推送精确 commit，服务器只从该 commit 建 release。
5. live `app.py`/静态资源 composite 基线与审计版本一致。
6. 生产 SQLite 在线备份完成，副本迁移、旧 queue/canary 查询和重复检测通过。
7. 素材池至少准备三条可验证的 Dramawave 素材；若不足三条，接受首轮整批不发。

## 部署步骤

1. 停止新代码变更，记录 Git commit、工作树状态和测试证据。
2. 只读核对生产服务、timer、三个账号、MySQL schema、数据盘和当前 release。
3. 备份 SQLite、Token 目录 hash/mode、env、unit、Nginx、静态页面和当前 release；不输出秘密内容。
4. 在 SQLite 备份副本运行迁移和完整测试，核对旧 run/queue/log/pool 计数。
5. 从 GitHub 精确 commit 建新 release，验证 Python 3.9、依赖和文件 hash。
6. 停止 Sidecar 的最小窗口内对 live SQLite 执行迁移；失败立即保持旧 release，不启用 timer。
7. 切换 Sidecar release 并窄重启；仅在 composite 基线一致时更新主后台和静态页面。
8. 验证管理员素材池查询，录入经人工确认的素材 ID；不手工创建 daily plan 或真实 Post。
9. 更新 daily env 的两个 pool 路径，验证 unit 后重启/启用 timer；首日用 start_date 门禁确认不会补发。
10. 核对 next trigger、journal 脱敏、素材池 FIFO、失败 run 记录和后台日志。

生产 `/usr/share/nginx/html/quick-nav.js` 与 GitHub release 存在已确认的导航 composite 差异。部署时必须备份并保留该 live 文件，不得用 release 版本整文件覆盖；只以结构化 JSON 方式在 live `navigation.json` 的 `x_platform.items` 中增量加入 `xPostMaterialPool`、把 `xPostLogs` 顺序调整为 40，并部署新页面。`/root/drama_material_service/static` 仍更新为精确 release 文件，便于主后台源码与回滚审计。

## 验证步骤

- Sidecar health 200；公网 internal 路由不可访问。
- 管理员素材池页面/API 200，普通用户/API Token/cross-origin 写请求拒绝。
- 管理员列表为池内素材附加安全 HTTPS `material_preview_url`，页面直接打开；不存在/非法 URL 显示“无法预览”，且不会修改池、queue 或日志。旧 302 接口仅做兼容。
- 批量添加重复或历史 queue 素材整批回滚。
- 临时未占用素材可删除；已占用/已发布素材返回 409。
- daily bearer 可访问 available/check，不能访问 query/add/delete。
- available 返回严格 `created_at,id` 顺序，非 Dramawave/违规/危险标签/媒体异常不进入计划。
- 只有三条全部通过时才出现三条 queue；不足三条时 Post 数为 0。
- 首轮自然 timer 后核对 queue/log/pool：成功项 published，known failure/unknown 保持 unpublished 且派生不可重发。
- 既有 canary、OAuth、短链、X 日志页面和账号权限回归正常。

## 回滚方案

1. `systemctl disable --now x-post-daily.timer`，立即停止新批次。
2. 保留当前 SQLite、短链、Token 和日志证据，切回上一精确 release 并窄重启。
3. 已产生任何新 queue/log/Post 后，不恢复部署前 SQLite，不删除新表/触发器；以修复前滚为主。
4. 若迁移后尚无任何新记录，可在停服和人工核对计数/hash 后恢复数据库备份。
5. 静态页面可回滚，但必须保留已发布 Post 对应的 `/s2l/{log_id}.html`。

## 注意事项

- 部署不是授权手工发帖；首个正式发布仍由确认后的自然 timer 执行。
- pool ID、queue ID、log ID 和 X post ID 都是审计证据，不做重编号或清理。
- 不提交或输出真实密码、OAuth Token、内部 bearer、数据库连接串。
- 失败和 unknown 不通过删池、改状态或重新入池处理，应在日志页人工核查。
