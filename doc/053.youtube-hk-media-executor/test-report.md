# 测试报告

开发阶段运行扩展回归，244 项通过；py_compile 与 diff-check 通过。

生产部署 commit `f6bef3fb8e68a51663d2b2ba24394fc0640df28c`。香港 release 与 media health 正常，CPU 隧道 health 正常，CPU publisher 已配置 remote executor；两端服务 active。SQLite `quick_check=ok`，发布记录切换前后均为 3，活动记录均为 0。未调用真实 YouTube 上传、评论或创建发布任务。

首次香港启动因 release 使用 7 位目录名而被身份门禁拒绝；改用完整 40 位 SHA 目录后通过，见 BUG-001。失败期间 CPU publisher 未启用新流量。
