# 实施计划

1. 保留独立日报原分支为回滚基线，复用 analytics/report.py 原文件。
2. 替换旧二字段严格匹配为唯一 campaign ID，纳入手动短链，源 SQL 按 BINARY 分组。
3. 更正版增加显式 CLI 选项、独立存档/账本/UUID、卡片说明。
4. QA 并行验证归属、守恒、发布量、缺数据、重复发送及未知发送保护。
5. GitHub 推送后，服务器精确 SHA 发布，先预览与 AI 逻辑对账，再切独立 current 并发送一次。

验证：python -m py_compile features/youtube_analytics/report.py features/youtube_daily_report/collector.py features/youtube_daily_report/report.py scripts/youtube_publisher_daily_report.py；python -m unittest discover -s tests -p test_youtube_daily*.py；git diff --check。
