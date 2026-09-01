# 部署文档

## 部署前门禁

1. 基于 exact Git commit 备份主 API/static/unit 并记录 SHA/回滚点。
2. 确认 `/usr/bin/mysql` 精确解析到 `/usr/local/bin/mysql-gated`；mysql.real、mariadb、其他二进制/路径和 PyMySQL 均不允许。
3. 确认只读 63350、`kunlunads_dev.ads_drama_bills.campaign` 和 `idx_site_event_time`；EXPLAIN 应保持已确认的约 8k 行 ref 扫描，不输出密码。
4. `install -d -m 0750 /mnt/data-disk/x-account-operating-stats` 并确认数据盘挂载。
5. 记录原 `/api/admin/x-accounts`、页面和 timer；保留 X SQLite/Token。

## 建议步骤

1. 部署 exact Git 的 module/script/app/static/env，安装 unit/timer；运行 `systemd-analyze verify`。oneshot 在 `ProtectSystem=strict` 下仅额外放行 SQL Gate 的既有 session-lock 目录写锁，不放行其状态账本或配置目录；为只读打开 `0600 x-post-automation` 账本仅保留 `CAP_DAC_READ_SEARCH`，不授予任何 DAC 写能力，Token 与 SSH 路径继续不可见。
2. 先手动启动 refresh oneshot（只读、无 X），核验 current.json mode/schema/time/金额。
3. 仅重启主 API，发布 static 到实际 Nginx docroot；管理员核验 API/UI/no-store。
4. 启用 timer，核对每天北京时间 10:00、11:00、12:00 三次触发；不得用真实 X Post 验收。

## 回滚

停用新增 timer/service，恢复 app/static/env/unit 并仅重启主 API。缓存可保留；禁止恢复/修改 X SQLite/Token。

## 2026-08-18 生产结果

- GitHub-first 分支 `codex/x-account-stats-20260818`；生产功能提交为 `9f84ee3c30ac372de7ffcc45ff385af99ecddbb7`，备份目录为 `/mnt/data-disk/x-account-operating-stats/backups/20260818-1528-pre-9c515ed`。
- refresh oneshot 成功，缓存为 `0640 root:root`；16 个账号、410 条已发布 Post、31 次 Repost，账本冲突 0。
- `site_id=2116` 收入合计 `$5,822.28`：可归属 `$5,092.57`，未归属 `$729.71`；昨日合计 `$637.09`，未归属 `$0.00`。
- `drama-material-api.service` 已仅重启主 API；`x-post-automation.service` PID 未变化。timer 已启用，下一次为 2026-08-18 21:10 CST。
- Nginx 配置、public page 200、未登录 admin API 401、生产静态资产的 1600/1280 布局验收均通过；未创建真实 X Post，未修改 X SQLite/Token/OAuth。
