# 部署与回滚

## 边界

CPU 43.166.187.96，数据盘 UUID 3e8ac4e8-7770-456d-9e89-2ec5dd405fa8。沿用私有 /etc/socialkit-analytics.env，不打印/改变凭据，无 DDL，不修改业务源表。GitHub 精确提交拉取到不可变 release。共享 API/导航、发布服务和原日报不变。

## 顺序

1. 测试审查后提交并推送 codex/socialkit-hourly-gap-alert-20261103，记录 COMMIT。
2. 验证数据盘、空间和 GitHub SSH；fetch 创建 releases/COMMIT，核对 HEAD、干净状态。
3. systemd 临时服务加载现有 EnvironmentFile，使用新 release 只刷新当前 UTC 日和前两日，不发送；完整校验后发布快照。
4. 固定版本执行 --preview，检查来源及候选；真实预览不进 Git。
5. python3 ops/socialkit-analytics/install_hourly.py --install COMMIT：停本 timer、持锁、备份/切换两处 HTML/JS、两个 unit、release 指针，不重启主 API。
6. 首次 --send --version VERSION，核对回执；systemctl enable --now socialkit-analytics-refresh.timer，验证下一整点及下一轮结果。
7. --verify-rollback MANIFEST 核对回滚包，记录实际备份路径、版本、回执。

## 回滚

从新 release 执行 python3 ops/socialkit-analytics/install_hourly.py --rollback /mnt/data-disk/socialkit-analytics/backups/hourly-TIMESTAMP/manifest.json。先核对当前文件/指针/备份哈希，再持锁恢复旧文件和指针，按原状态恢复 timer。提醒 SQLite、中间表、快照保留，不重试未知发送。后续文件已变化时禁止整份覆盖，先评估差异。

切换失败保持 timer 停止并保留备份，恢复或修复后再启用。刷新正在运行不得强杀，等本轮结束再部署。
