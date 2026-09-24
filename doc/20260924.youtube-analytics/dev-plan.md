# 实现计划

- features/youtube_analytics/report.py：只读维度读取、归因唯一性、筛选聚合、CSV。
- routes.py：Cookie/导航权限、限定SQL、查询并发/缓存与API。
- app.py：只新增报表独立路由；quick-nav.js/navigation.json新增入口。
- static/youtube-analytics.*：共享顶栏导航、筛选/指标/图表/分组明细。
- scripts/deploy_youtube_analytics.py：GitHub精确提交，校验live hashes，备份代码及SQLite，原子替换两份静态，只重启API，失败恢复，代码回滚保留数据库。

并行前端与QA，主开发完成后端/部署。实际检查记录在test-report及deployment-evidence。
