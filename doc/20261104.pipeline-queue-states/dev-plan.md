# 实施与验证

隔离分支 codex/pipeline-queue-states-20261104，基线 b58a5257ff43ccd7edbfc19f8eb2cd95c312a809。工作树不修改其他会话目录。

1. 新增只读 queue_states 分类并接入 summary。
2. 顶部与渠道表统一四类统计，异常显示原因明细，旧 DTO 不错误套用旧排队数。
3. Python 24 项测试与真实 Chromium 本地接口模拟，权限、旧链接、实时容量表单、15 秒刷新保持。
4. 线上内存加载候选模块，仅通过 mode=ro + query_only 验证。
5. GitHub 精确提交后，以内容守卫窄部署。协调器仅排空 HTTP 控制请求后短暂重启；执行器不重启，不停任务，不改容量或渠道开关。

命令：python -m unittest scripts.test_pipeline_queue_states scripts.test_pipeline_monitor；python -m py_compile features/pipeline_dispatch/queue_states.py features/pipeline_dispatch/views.py scripts/deploy_pipeline_queue_states.py；node --check static/pipeline-monitor.js；node scripts/test_pipeline_monitor_ui.js；git diff --check。
