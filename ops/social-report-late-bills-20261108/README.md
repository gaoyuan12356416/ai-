# 社媒日报账单延迟修复

2026-11-08 11:00 日报先于前日账单入库（11:04:03–04），完整性校验阻止发送。账单到齐后通过原服务补发，11:41 飞书 GET 回查成功。

兼容入口仅在采集未完成时退出 75；现有服务每 300 秒重试，1 小时内最多 7 次启动。显式配置/代码错误 1、发送失败或未知 2 不自动重试。保持 11:00 定时、日期/群发送账本与未知结果保护；不改发布服务与收入统计口径。

验证：`python3 -m unittest discover -s tests -p 'test_social_daily_*.py' -q`。
推送并 fetch 验证提交后运行本目录 deploy.py stage；候选版本 --preview 检查完整性后 apply。检查 systemctl show 的 Restart、RestartUSec、RestartPreventExitStatus、StartLimitBurst、StartLimitIntervalUSec 与 timer。
回滚：本目录 deploy.py rollback。仅回滚代码指针和重试 drop-in；不恢复发送账本、不重复发送历史日报。
