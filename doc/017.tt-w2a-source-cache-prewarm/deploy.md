# 部署与回滚

## 变更内容

- 新增 W2A 原始 HTML资源模块和 SQLite 持久缓存。
- resolver 元数据源切换为共享资源服务。
- 新增最近 3 日投放剧轮转预热脚本、oneshot 和每 4 小时 timer。
- featured 排名逻辑不变，MySQL 仅查询昨日花费；资源元数据改为共享缓存。

## 关键配置

配置名以最终实现和 `.env.example` 为准，计划包含：

- `TT_DRAMA_RESOURCE_DB_PATH=/mnt/data-disk/tt-drama-resource-cache/state/resources.sqlite3`
- `TT_DRAMA_RESOURCE_SOURCE=w2a_cache`
- `TT_DRAMA_RESOURCE_LANDING_ID=2049`
- `TT_DRAMA_RESOURCE_POSITIVE_TTL_SECONDS=86400`
- `TT_DRAMA_RESOURCE_NEGATIVE_TTL_SECONDS=900`
- `TT_DRAMA_RESOURCE_STALE_TTL_SECONDS=604800`
- `TT_DRAMA_RESOURCE_HTTP_TIMEOUT_SECONDS=5`
- `TT_DRAMA_RESOURCE_HTTP_MAX_BYTES=524288`
- `TT_DRAMA_RESOURCE_LEASE_SECONDS`
- `TT_DRAMA_RESOURCE_WAIT_TIMEOUT_SECONDS`
- `TT_DRAMA_RESOURCE_COVER_HOSTS=cdn.usrgrow.com,...`
- `TT_DRAMA_RESOURCE_PREWARM_CURSOR_PATH=/mnt/data-disk/tt-drama-resource-cache/state/prewarm-cursor.json`
- 预热固定 `kunlunads_dev.ads_custom_source_insight` 表和 `as` 索引、3 个上海自然日、普通批次硬上限 500、4 workers、2 QPS、候选硬上限 5000；只有显式 bootstrap 可到 3000。

## 数据库变更

- 远端 MySQL：无 DDL、无 DML；预热和 featured 只读固定的 `kunlunads_dev.ads_custom_source_insight`，并强制使用已核验索引 `as`，配置不能扩展到其他表或索引。
- 本地 SQLite：首次启动创建资源、租约和 meta 表；资源与租约均以 `(landing_id, content_id)` 为复合主键，不持久化源 URL、原始 HTML或完整深链。
- 首次 SQLite 初始化完成完整 mountpoint/UUID 校验并记录 state 父目录 `st_dev`；长驻 API 每次 connect 前后复查完整路径无软链接、父目录仍为目录且设备号未变，变化立即关闭连接并 fail closed。
- 备份活动 SQLite 时使用 SQLite 在线备份能力或受控停写，不直接复制活动 WAL 数据库。

## GitHub-first 部署步骤

1. 在本地完成代码、文档、单元测试和 SA 代码评审。
2. 提交并推送 GitHub 分支，记录精确 commit SHA；工作区未提交文件不得进入生产。
3. 只读记录当前生产版本、服务/timer、数据盘、Nginx、运行文件哈希和 resolver/featured 基线。
4. 在数据盘建立本次 backup 与 release 目录，并从 GitHub 检出精确 commit；生成 release manifest。
5. 验证 `/mnt/data-disk` 为独立挂载、UUID 正确、空间充足且目标路径不是软链接，再创建：

   ```text
   /mnt/data-disk/tt-drama-resource-cache/state
   /mnt/data-disk/tt-drama-resource-cache/backups
   /mnt/data-disk/tt-drama-resource-cache/releases
   ```

6. 安装共享资源模块、预热脚本、featured 刷新脚本、环境示例和 systemd unit；`tt-drama-resource-prewarm.service` 与 `tt-drama-featured.service` 从 `/mnt/data-disk/tt-drama-resource-cache/current` 执行。部署及离线 unit 使用固定 `/usr/bin/install -d -m 2770 -o tt-drama-featured -g tt-drama-featured /mnt/data-disk/tt-drama-resource-cache/state` 创建 state。主 API drop-in 不得设置全局 `UMask`；缓存模块负责把 DB、`-wal`、`-shm` 显式规范为 `0660`，避免改变单体后台其他文件权限。
7. 执行 `py_compile`、目标单测、SQLite schema 检查和 `systemd-analyze verify`；在 Linux 运行 storage identity 测试，覆盖首次设备号记录、connect 前变化及 connect 建立后的竞态变化。
   - 预检环境必须同时满足 `TT_DRAMA_RESOURCE_SOURCE=w2a_cache` 和 `TT_DRAMA_RESOURCE_LANDING_ID=2049`；不满足时停止发布。`app.py` 的 MySQL 安全回退只用于避免单体 API 导入崩溃，不能替代生产配置验收。
8. 使用少量已知有效/错误 ID 手工预热，验证只发生 HTML GET、精确 ID 校验和缓存持久命中。
9. 将 resolver/featured 接入新服务，重启 `drama-material-api.service`；如 Nginx 未变更则不 reload。
10. 验证公网页面的成功、404、503、stale、参数透传、featured 5 条和移动端点击。
11. 先运行预热 `--dry-run`，确认不访问 W2A、不写 SQLite/cursor；再手工运行一次 oneshot，检查固定 insight 表/索引、花费排名未被 ID 重排、cursor schema v2 的 `next_content_id`/`next_index` 接续、有界 retry backlog、普通 500 硬上限、新鲜命中跳过、SQLite 行数、journal 和锁。bootstrap 仅在显式受控执行时允许，必须从最高花费候选开始且不得超过 3000。
12. 最后 enable/start timer，确认基准触发为上海时间 `00/04/08/12/16/20:20`，并核对 `Persistent=true` 与 `RandomizedDelaySec=5m`。
13. 比对 GitHub SHA、release manifest、生产运行文件和 systemd unit 哈希，补写真实部署记录。

## 发布验证

- `systemctl is-active drama-material-api.service`
- `systemctl status tt-drama-resource-prewarm.service --no-pager`
- `systemctl list-timers tt-drama-resource-prewarm.timer --all --no-pager`
- `journalctl -u tt-drama-resource-prewarm.service -n 100 --no-pager`
- `systemctl show drama-material-api.service -p NRestarts`
- `nginx -t`
- resolver 200/400/404/429/503、`no-store`、缓存头和 `Server-Timing`；公开头将内部 `ORIGIN_FILL/NEGATIVE_FILL` 映射为 `MISS`、`DISK_HIT` 映射为 `HIT`
- 正常/错误 ID 的源码实际 ID 精确比对
- 进程重启前后 SQLite 缓存命中
- 长驻实例的已记录 parent `st_dev` 与当前 state 父目录一致；Linux 测试证明 connect 前或后发生目录/设备/软链接变化时连接被关闭、请求 fail closed，且根盘没有新增 SQLite 文件
- `prewarm-cursor.json` schema v2 原子推进，保持花费排名；候选变化时优先按 `next_content_id` 接续，ID 消失时使用 `next_index` 兜底；失败积压有界且每轮仍保留正常轮转位置
- `systemctl cat drama-material-api.service` 确认 TT 缓存 drop-in 未设置全局 `UMask`；state 目录 owner/group 为 `tt-drama-featured`、mode 为 `2770`
- `stat` 确认 `resources.sqlite3` 及已生成的 `resources.sqlite3-wal`、`resources.sqlite3-shm` 均为 mode `0660`；重启 API 和离线任务后权限保持不变
- featured 公共 JSON 恰好 5 条、无 spend、刷新失败 hash 不变
- 390×844 真实浏览器搜索、卡片和跳转参数验证

## 回滚方案

1. 立即 disable/stop `tt-drama-resource-prewarm.timer`，等待正在运行的 oneshot 结束或受控停止。
2. 紧急切换时先将 `TT_DRAMA_RESOURCE_SOURCE=mysql` 并重启主 API；完整回滚再从上一 GitHub commit/release 恢复 resolver、featured、`app.py`、脚本和配置，不使用本地未提交文件。
3. 恢复或移走本次新增 systemd unit，执行 `daemon-reload`。
4. 执行 `py_compile`，重启 `drama-material-api.service`。
5. 验证旧 resolver 的成功/404、featured LKG、`/tt` 搜索和 CTA。
6. 保留 SQLite、journal、release 和备份用于审计，不递归删除数据盘缓存；恢复后旧代码不会依赖该文件。
7. 若 SQLite schema 变更导致回滚版本不兼容，恢复上线前在线备份副本，而不是手工修改表。

## 发布门禁

- 未完成 SA 代码评审、测试报告或仍有 P0/P1，不发布。
- 生产文件不能追溯到已推送 GitHub commit，不发布。
- 数据盘门禁、精确 ID 校验、错误不负缓存或 featured LKG 任一失败，不发布。
- Linux 的 POSIX mode preservation 或 connect 前后 storage identity 测试未通过，不发布。
- 未记录原版本和可执行回滚点，不发布。

## 实际部署记录

当前预发布实现、SA 代码评审和本地门禁已完成，证据见 `test-report.md`；Linux POSIX mode preservation 与全部生产验证仍待执行。待生产部署后填写 commit、release、backup、manifest、SQLite schema、timer、性能和回滚点；当前没有 commit 或部署证据。
