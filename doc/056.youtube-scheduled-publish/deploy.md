# 部署与回滚

目标 CPU 43.166.187.96，/root/drama_material_service；公网页面 /usr/share/nginx/html/youtube-publish.*。

使用 scripts/deploy_youtube_schedule.py，GitHub 40 位提交与 .github-verified-commit 必须匹配。安装前校验被改文件 SHA 和独立 YouTube handler SHA；app.py 仅替换目标方法，保留其他业务线上代码。核心 SQLite 采用 additive migration，备份仅作审计不得覆盖新视频事实。

仅在所有发布与生图均空闲时部署，停止 API/legacy/reviewed worker 后再次核验空闲，备份代码及两个 SQLite；安装、初始化新列、启动原服务，统一 writer 不重启。回滚先检查不存在未完结预约/未知预约意图，否则拒绝回退旧引擎；保留数据库、预约事实、资产、通知、短链。精确提交、备份、验收结果将在上线后补充。
