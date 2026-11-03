# 实施计划

并行进行监控修复、FB 三条历史回执只读核对、TT 八条账号阻塞核对。根代理整合评审、测试、GitHub 提交和窄部署。

基础版本 a97fc231e37d869df26c59a8af623217799c9024，干净工作树 pipeline-unblock-20261103。生产只部署本改动涉及的文件，不回退其他聊天已上线功能。

使用 Python unittest、py_compile、node --check 和现有 monitor JS 测试。管理员恢复工具必须具有默认 dry-run、证据哈希、CAS、事务、重复执行和反例覆盖。实际结果见 test-report.md。
