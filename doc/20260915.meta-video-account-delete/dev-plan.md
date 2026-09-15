# 开发计划

隔离目录D:/codex/worktrees/meta-video-account-delete-20260915，分支codex/meta-video-account-delete-20260915，基线ecb72d9（运行b0d5790）。原工作区及其他模块不覆盖。

Graph/Source/Bridge负责账户请求、来源User Token及只读核实；Store负责账户尝试、回执和重启；Service负责固定范围、逐账户执行及真实SQLite编排测试；UI负责账户结果及安全诊断。

实现后执行python -m unittest discover -s tests -p 'test_fb_ad_asset_delete_*.py' -q、改动模块/脚本py_compile、node --check static/fb-post-ad-delete.js、前端DOM模拟、git diff --check。独立代码审查后GitHub提交，再从精确版本部署。
