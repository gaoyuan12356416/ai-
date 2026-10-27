# 实施计划

1. 从线上相同文件 SHA 的 social-web-ai-scope 分支建立独立工作树。
2. 提取共享 selected 谓词；新增 scoped_metrics；网页路由接入，日报保持原调用。
3. 运行 python -m unittest discover -s tests -p 'test_social_*.py'，编译、diff 检查。
4. GitHub 提交推送后服务器 fetch 精确提交；隔离 release 运行测试和真实只读候选查询。
5. SHA 守卫备份并仅更新三个模块，重启主 API，核对依赖服务 PID。
6. 真实管理员会话 GET 报表/CSV并用浏览器查看指定条件。
