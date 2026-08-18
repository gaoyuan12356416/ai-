# 部署文档（待单独授权）

## 部署前门禁

1. 基于 exact Git commit 备份主 API/static/unit 并记录 SHA/回滚点。
2. 确认 `/usr/bin/mysql` 精确解析到 `/usr/local/bin/mysql-gated`；mysql.real、mariadb、其他二进制/路径和 PyMySQL 均不允许。
3. 确认只读 63350、`kunlunads_dev.ads_drama_bills.campaign` 和 `idx_site_event_time`；EXPLAIN 应保持已确认的约 8k 行 ref 扫描，不输出密码。
4. `install -d -m 0750 /mnt/data-disk/x-account-operating-stats` 并确认数据盘挂载。
5. 记录原 `/api/admin/x-accounts`、页面和 timer；保留 X SQLite/Token。

## 建议步骤

1. 部署 exact Git 的 module/script/app/static/env，安装 unit/timer；运行 `systemd-analyze verify`。oneshot 在 `ProtectSystem=strict` 下仅额外放行 SQL Gate 的既有 session-lock 目录写锁，不放行其状态账本或配置目录。
2. 先手动启动 refresh oneshot（只读、无 X），核验 current.json mode/schema/time/金额。
3. 仅重启主 API，发布 static 到实际 Nginx docroot；管理员核验 API/UI/no-store。
4. 启用 timer，核对下一次北京时间 09:10/21:10；不得用真实 X Post 验收。

## 回滚

停用新增 timer/service，恢复 app/static/env/unit 并仅重启主 API。缓存可保留；禁止恢复/修改 X SQLite/Token。

当前未部署、未推送、未运行生产收入查询、未创建真实 X Post。
