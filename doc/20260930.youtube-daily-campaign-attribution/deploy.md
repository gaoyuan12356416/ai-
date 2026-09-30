# 部署与回滚

服务器 43.166.187.96，独立入口 /opt/youtube-publisher-daily-report/current。

从 GitHub 拉取精确提交到 /mnt/data-disk/youtube-publisher-daily-report/releases/<SHA>，先运行测试和真实昨日 --preview。复用 analytics 模块 SHA 必须与已部署 AI 后台相同。核对原回执/日报哈希，备份 delivery.sqlite3，确认 oneshot 空闲。

python3 <release>/scripts/install_youtube_publisher_daily_report.py --sha <SHA> 保存旧 current 与 unit 后切换。11:00 Asia/Shanghai timer 保持启用；不重启 API 或发布器。

显式发送：python3 <release>/scripts/youtube_publisher_daily_report.py --date 2026-09-29 --correction campaign-id-v2 --send。读回 Feishu message、sent ledger，重跑验证 already_sent；原日报哈希不变。

回滚：python3 <release>/scripts/install_youtube_publisher_daily_report.py --rollback <backup>。回滚保留日报和投递状态，不撤回或重复发送已送达消息。具体 SHA、backup 与结果见 deployment-evidence.md。
