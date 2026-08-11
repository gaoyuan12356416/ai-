# 部署文档

## 变更内容

- 短剧池未分配候选高优/取消高优。
- 素材池不入池手动异步发布批次。
- additive SQLite schema、Sidecar/main API、静态页面、manual runner 和 timer。

## 配置项

- `x-post-manual.service` 复用 `/etc/x-post-schedule.env` 的只读素材库、媒体、GPU repair、Sidecar URL/token 和共享锁配置。
- 不新增生产 X/OAuth secret；daily bearer 只增加处理 backend 已创建 manual run 的精确内部权限。

## 数据库变更

- `x_post_drama_pool` 增加三个高优审计字段。
- 新建 `x_post_manual_run`。
- `x_post_queue` 增加 `manual_run_id` 和唯一索引/完整性触发器。
- 所有迁移可重复执行，不删除/重建历史表。

## 部署步骤

1. 记录生产 release、文件 hash、unit/timer 状态、queue/log/run/pool 计数和未决结果。
2. 验证 `/mnt/data-disk` 挂载 UUID/空间。
3. 创建 SQLite 在线备份、token 目录副本、配置/unit/主后台/公网静态文件和当前 release 备份；生成并验证 manifest。
4. 在在线备份副本运行两次新 `ensure_storage()` 和测试，比较历史行、token hash/mode。
5. 从 GitHub 拉取精确提交并构造不可变 `/mnt/data-disk/x-post-automation/releases/<commit>`。
6. 安装/校验 manual unit/timer，先保持 timer 停止。
7. 在自然无发布窗口停止 `x-post-automation.service`，切换 release，执行 additive migration并立即恢复 Sidecar。
8. 部署主后台 `app.py`、client/service/selector 和两份静态页面，只重启 `drama-material-api.service`。
9. 启用 manual timer；不启动 manual service，不点击手动发布。

## 验证步骤

- `systemctl status x-post-automation.service drama-material-api.service x-post-manual.timer`
- Sidecar/public health、匿名 401/no-store、两页 HTTP 200。
- SQLite `integrity_check=ok`、schema/索引/触发器存在、历史计数和未决结果不变。
- 使用生产 DB 在线备份副本或独立临时 DB 调内部状态机测试；禁止真实 X client。
- 浏览器只检查按钮、弹窗、高优可用条件和请求构造，不确认真实发布。
- 部署前后 queue/log 中 X post ID 数量不增加，journal 无 token/secret。

## 回滚方案

- 保留 `/var/lib/x-post-automation` 新 schema、manual run、queue/log 和 token 状态。
- 将 `/opt/x-post-automation/current` 切回部署前 release，恢复备份的主后台/静态/unit 文件，`daemon-reload` 后仅重启 Sidecar/main API。
- 禁用并移除 manual timer 代码入口，但不删除历史表/列。
- 精确命令和备份路径在实际部署后补记。

## 注意事项

- 不以真实 Post 作为部署 canary。
- 如部署时已有手动 run/queue，回滚前先只读核查；不得删除或重放。
- 如任何 publish log 为 unknown/post_creating，停止后续部署动作并人工对账。

## 2026-08-11 实际部署记录

- 部署提交：`5f9084b59bb14d1efd806ed32d070a6b2ee851c1`。
- 前一 release：`29bd90034396c597b30ceb7135376efb750ec886`，仍完整保留。
- 当前 release：`/mnt/data-disk/x-post-automation/releases/5f9084b59bb14d1efd806ed32d070a6b2ee851c1`。
- 备份：`/mnt/data-disk/x-post-automation/backups/20260811T113511+0800-x-post-priority-manual-74e7d50`；原始 `SHA256SUMS`、在线 SQLite 备份、rollback tar 和部署结果均已校验。Nginx 公网页面修正前的 3 个旧文件另存为 `public-static-before-correction.tar`，SHA256 为 `ba11b41c5d76e9d7a01676d2c2941ee884b5588917e68a53e5b812cdf62799e0`。
- 精确提交的生产数据库副本连续执行两次迁移：旧表全部行的原字段哈希未变，`integrity_check=ok`，第二次 schema 与第一次相同，新表/关联为空。
- 服务：Sidecar、主后台、schedule timer、claim timer、manual timer 均 `active/enabled`；manual service 由 timer 自然执行并返回 `no_pending`，没有人工启动。
- 部署前后均为 queue `150`、log `150`、确认 Post ID `149`、unknown/post_creating `0`、活跃 queue `0`；本次验收未创建真实 X Post。
- Nginx 对 3 个 X 页面直接使用 `/usr/share/nginx/html`；已从精确 release 补齐公网静态文件，并逐个 `cmp`/SHA256 对齐。三个页面 HTTP 200；匿名手动任务查询 HTTP 401 且 `Cache-Control: no-store`。
- 登录态 Chrome 只读验收：素材池“手动发布”可见且在“加入素材池”上方，弹窗警告和二次确认可见；短剧池 12 行均有“高优”，7 行符合条件可用、5 行按业务规则禁用。仅打开并关闭弹窗，没有点击最终确认。
- 浏览器验收后数据库仍为 queue/log/Post ID `150/150/149`，manual run `0`、manual queue link `0`、高优记录 `0`、外键违规 `0`。

### 精确回滚点

1. 停止 manual/schedule/claim timers，并停止 Sidecar。
2. 将 `/opt/x-post-automation/current` 原子切回前一 release。
3. 从备份 `rollback-files.tar` 仅恢复 `app.py`、X accounts client、应用静态页面和原 systemd unit；再从 `public-static-before-correction.tar` 将 3 个公网 X 页面恢复到 `/usr/share/nginx/html`。manual unit 保持禁用。
4. `daemon-reload` 后启动 Sidecar、主后台和原两个 timer。
5. 保留 additive schema 和任何后来产生的 manual run/queue/log，不删除、不重放；按备份 `baseline.json` 与部署结果重新核对账本。

## 2026-08-11 手动超长原片修复部署记录

- 生产根因：手动 selector 复用了自动素材池的 `video_duration<=600` SQL 条件；服务层又把 Ads promoted-video 的 600 秒边界误作 token 确认 Premium 账号的有机 Post 上限。
- 用户授权的唯一真实边界 canary 使用账号 `13`、素材 `5286820`。原片实际 `763.938005` 秒、`162253615` 字节、720×1280、30fps、H.264/AAC；以 `amplify_video` 发布成功，queue/log `151`，X Post `2087081262754169202`，回读 `duration_ms=763938`、`unknown_outcome=0`。
- canary 前备份：`/mnt/data-disk/x-post-automation/backups/20260811T073629Z-raw-763s-canary`，包含 SQLite 在线快照、15 个 token 文件、运行配置和原 release 链接。
- 修复先提交为 `513af93d381ccfbb7faf167747c2f13e7dc326ad`，发现并行生产 release 已前进到 `70768ec1741caad08a9dd49ba11b9639839dad14` 后，以 merge commit `fc2377460da20ef59e53082609c421a79de1e972` 合并当前生产头再部署，未覆盖并行变更。
- 当前 release：`/mnt/data-disk/x-post-automation/releases/fc2377460da20ef59e53082609c421a79de1e972`；上一 release `70768ec1741caad08a9dd49ba11b9639839dad14` 完整保留。
- 部署备份：`/mnt/data-disk/x-post-automation/backups/20260811T075304Z-manual-duration-fc237746`；SQLite SHA-256 为 `d78c48e1de46fb350c0afa308af57d2169eec11cd5267343503899bf42c9fec7`，token 文件 15 个，另含主后台 overlay、公网页面、配置、units 和原 release 链接。
- Windows 合并后回归：X Post `330` 项通过、`1` 项按设计跳过；X 账号/内部接口 `89` 项通过。Linux 目标 release 的本次聚焦回归 `119/119`、账号/接口 `89/89` 通过。Linux 全量测试中的 3 个失败/22 个错误与前一生产 release 完全同型，均源于测试夹具 `/tmp` 与 Linux 固定数据盘目录门禁冲突，不是本变更新增。
- 切换时先持有 `/run/x-post-daily/runner.lock`，再短暂停止 manual/schedule/claim timers 与依赖 oneshot，原子切换 Sidecar release，并同步主后台 `service.py`、`selector.py` 和两份素材池静态页。所有失败的部署编排尝试均由 trap 自动切回旧 release；失败点仅为 systemd 并发事务、中文 SSH 校验字面量和一次 `curl` 参数拼写，账本在每次回滚后均未变化。
- 最终验收：Sidecar cwd 指向新 release，health `ok`；主后台与公网文件 SHA-256 均与 release 一致，公网 HTTP 200；运行策略为 `PREMIUM_MAX_DURATION_SECONDS=14400.0`，手动 selector 参数为 `allow_long_duration=False`（仅手动调用显式开启）。
- 部署前后均为 queue `151`、log `151`、published/确认 Post `150/150`、unknown `0`、活跃 queue `0`，`integrity_check=ok`；没有第二次真实发布。生产批次 `#3` 保持 `failed_preflight` 且未重放，canary log `151` 保持 `published`。
- 自然 timer 证据：manual timer 连续返回 `no_pending`；schedule/claim 正常 `Succeeded`，三个 timer 均 `active/enabled`。

### 本次精确回滚点

1. 获取共享发布锁并停止 manual/schedule/claim timers 与依赖 oneshot。
2. 停止 Sidecar，将 `/opt/x-post-automation/current` 原子切回 `70768ec1741caad08a9dd49ba11b9639839dad14`。
3. 从 `20260811T075304Z-manual-duration-fc237746` 仅恢复主后台 `features/x_posts/service.py`、`selector.py`、应用静态页和 Nginx 公网页面；不恢复 SQLite 或 token。
4. 启动 Sidecar、重启主后台并恢复三个 timer；重新核对 queue/log/Post/unknown 与 SQLite 完整性。

## 2026-08-11 短剧高优 PUT 502 修复记录

- 真实请求：15:42:54、15:43:03 对池记录 `137` 的两次
  `PUT /api/admin/x-posts/drama-pool/137/priority` 均由 Nginx 返回 502；主 API
  traceback 定位到 `app.py#do_PUT` 的 `NameError: name 'urllib' is not defined`。
- 状态确认：两次请求均未到达 Sidecar，池 `137` 保持 `pending`、未绑定且
  `priority_at=''`，当时高优记录数为 `0`，无半成功状态。
- GitHub 修复提交：`ae0b5a99f29506a4651b2c860d3a3a1306d787b8`；精确源 checkout：
  `/mnt/data-disk/x-post-automation/releases/ae0b5a99f29506a4651b2c860d3a3a1306d787b8`。
  checkout 的父提交 `app.py` 与部署前主 API 文件逐字节一致，新文件仅有一行替换，
  SHA-256 为 `2cf6be5f28ba6ec3039cb2b596ec4cc9d418d10f89939808324b904ff3488dac`。
- 回滚备份：
  `/mnt/data-disk/x-post-automation/backups/20260811T081818Z-drama-priority-put-ae0b5a9`；
  包含旧 `app.py`、主 API unit、服务/账本基线、SQLite 在线备份和 15 个 Token 的
  非敏感 hash/mode/owner 清单，`SHA256SUMS` 与 `integrity_check=ok` 均通过。
- 本地验证：`app.py` 编译通过；路由合同 27 项、X Post 330 项（1 项既有环境门禁
  跳过）、X 账号 58 项通过。Linux 精确 checkout 的路由合同 27 项通过。
- 仅替换 `/root/drama_material_service/app.py` 并重启
  `drama-material-api.service`。首轮验收脚本错误地预期匿名请求返回 403，实际正确合同
  为 401；trap 已自动恢复旧文件并重启，确认账本不变后按正确合同重新部署成功。
- 部署后 loopback 与公网匿名 PUT 均稳定返回 `401 auth_required`，不再断链或 502；
  新主 API PID 日志无 traceback，公网短剧池页面 HTTP 200。
- Sidecar PID 与并行前进的 release
  `e4d0b032491c271591ce7016d3559c8af6105073` 均未改变；manual/schedule/claim
  timers 保持 active。部署即时账本仍为 queue/log/确认 Post `151/151/150`、unknown
  `0`、活跃 queue `0`，本修复没有创建 Post。
- 部署完成后，另一管理员“苏斯琪”独立创建 manual run `4`，其 queue/log `152`
  在 16:25:07 正常完成，最终全局计数变为 `152/152/151`、unknown/active `0`；该
  Post 与部署、高优接口验收无关。
- Chrome 扩展在确认池 `137` 高优后、发出 PUT 之前超时；Nginx、Sidecar 和 SQLite
  均证明没有新增高优请求。为避免重发，池 `137` 有意保持未高优，交由管理员在页面
  重新点击一次。

### 本次精确回滚点

仅回滚主 API 文件：将备份中的 `app.py.before` 以 `0644 root:root` 原子恢复到
`/root/drama_material_service/app.py`，然后只重启 `drama-material-api.service`。
不要切换 Sidecar release、恢复 SQLite/Token 或停止发布 timers。

## 2026-08-11 素材池发布设置 PUT 404 修复记录

- 16:47:53、16:47:57、16:48:02 三次
  `PUT /api/admin/x-posts/material-pool/schedule` 均返回 404
  `not_found`。素材配置保持版本 `16` 和旧更新时间，证明没有部分保存。
- 根因是主 API 的 `do_PUT()` 白名单只包含短剧高优路径，漏掉素材池和短剧池
  两个实际由页面使用的 schedule PUT。
- GitHub 修复提交：
  `e248b757215f62fd73194060c72363002a93355e`；生产 checkout：
  `/mnt/data-disk/x-post-automation/releases/e248b757215f62fd73194060c72363002a93355e`。
- 回滚包：
  `/mnt/data-disk/x-post-automation/backups/20260811T090300Z-x-post-schedule-put-e248b75`；
  在线 SQLite、旧主 API、unit、服务/账本基线、Token 非敏感清单和 manifest 均已校验。
- 仅部署 `/root/drama_material_service/app.py` 并重启主 API。新 SHA-256：
  `80703d90bc4cc80e527ff550de410ebda0e522055a7454bd99d8e5d7fd902570`；
  Sidecar 保持 PID `2617520` 和独立 release
  `1d60a8b09d0dcf7100490c56b1ac9b19217cf6e8`。
- 匿名素材/短剧 schedule PUT 及高优 PUT 均为结构化 401，未知 PUT 仍为 404，
  主 API PID `2626926` 无 traceback。
- 登录态 17:05:51 原请求重试返回 HTTP 200；素材配置升级到版本 `17`，
  新模板 SHA-256 为
  `c2d348c42fdf1b123133b549fc8efaed433cbc93060f52ab49a4e16b6e613911`，
  从 2026-08-12 生效，次日计划为 `02:33,15:42,21:08`。
- 当天版本 16 的已冻结计划未改写。自然 scheduler 在保存前 36 秒已为旧
  09:59 批次建立队列，最终完成五条独立 Post；它不是部署或保存触发的 canary。
  最终账本为 queue/log/published `162/162/161`，unknown/active `0/0`，
  `integrity_check=ok`，Token hash/mode/owner 清单未变。

### 本次精确回滚点

只恢复备份中的 `app.py.before` 并重启 `drama-material-api.service`。
保留版本 17 配置、现有 SQLite/Token、当前 Sidecar release 和所有历史队列；
不得通过回滚重画计划、删除队列或重放任何 Post。
