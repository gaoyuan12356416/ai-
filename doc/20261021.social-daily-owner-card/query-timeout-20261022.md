# 11 点日报查询超时修复

2026-10-22 11:00 任务按时触发，11:01:05 以 `ValueError` 退出，未生成正式批次或发送回执。原始日志未保存数据源细节。随后在相同 systemd 沙箱复现：Facebook 2049 查询两次失败，其余数据源完整；单源诊断返回 MySQL `3024 / HY000`，SQL Gate 等待仅 0.001 秒，实际触发的是 SQL 的 8 秒 `MAX_EXECUTION_TIME`。

独立日报现在使用单次 SQL 20 秒、Gate 执行 24 秒、进程最长 37 秒。继续使用只读 63350、FIFO Gate、最多两次读取和整个采集 120 秒预算。网页/API 未启用新的内部配置，默认仍为 8 秒；主 API 不随本次部署变更。失败仍阻止冻结和发送，不用缺失数据生成零值日报。

正式采集不完整时，将安全的失败原因、质量状态和检查时间保存到数据盘 `failures/<UTC统计日>/<时间>.json`，日志提供诊断路径。MySQL 3024 单独分类，不输出 SQL、凭据或原始 stderr。

卡片格式、AI 后台来源范围、campaign ID 归属、UTC 日期及所有发送去重规则保持一致。当天补发使用已经批准的完整预览，原发送账本和消息保持不变；上线验证仅重算预览，正式入口应返回 `already_sent`。

使用现有 `deploy_social_daily_owner_card.py` 分阶段部署，生产基线为 `890312d025dff43d1582fb33b53e015a77d1a8fc`。只切换独立日报 release；保留正式账本备份、原 release 和 deployment.json。激活前使用生产相同的 systemd 沙箱验证完整采集；回滚使用 `--rollback <deployment.json>`，不还原数据库或重新发送历史。

验证：`python -m unittest discover -s tests -p 'test_social*.py' -q`、语法编译、`git diff --check`；生产同环境完整预览及正式入口去重检查。此修复为有界查询提供更多执行时间，不保证未来任何数据库故障均能自动恢复。
