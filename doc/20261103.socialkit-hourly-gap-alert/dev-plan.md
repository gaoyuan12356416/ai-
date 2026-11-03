# 开发计划

1. 以既有 SocialKit 分支建立隔离工作树；将 61a4a5c 的共享社媒模块及测试纳入 release。八个 Python 文件与生产主 API 的 UTF-8/LF SHA-256 一致，YouTube 查询依赖亦一致。
2. 小时协调器选择 UTC 3 日或 15 日窗口，成功记录回算日期，调用独立提醒 CLI。手工 refresh --start/--end 不发送。
3. 独立采集、比较、分卡、SQLite 账本及 CLI；保留来源完整性及新鲜度。
4. 更新页面与 timer；窄安装器只替换两处 HTML/JS、两个 unit 和 release 指针。
5. 回归、独立审查、真实源预览、GitHub 提交部署、首发及下一周期核验。

检查：python -m unittest discover -s tests -p 'test_socialkit*.py' -v；社媒归因相关测试；Python compileall；node --check static/socialkit-analytics.js；git diff --check；服务端 Python 3.9 检查。
