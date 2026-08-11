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
