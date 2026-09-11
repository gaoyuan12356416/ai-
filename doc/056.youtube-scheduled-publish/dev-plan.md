# 开发计划

1. 前端增加北京时间选填时间、摘要、预约状态、改期/立即/取消及幂等草稿。
2. Preparation 存储时间、版本、过期恢复点，worker 驱动过期状态；新增权限一致的 POST schedule。
3. Reviewed 引擎与共享账本增加预约字段、持久化 intent、平台回读及轮询。旧 worker 仍只领取 legacy lane。
4. 原异常通知 observer 识别错过预约，复用原 outbox，禁止第二发送器。
5. 独立 SA 审查并发、未知结果、边界日期和取消事实。执行 Python unittest、Node 语法和 Playwright mock 流程。
6. GitHub 精确提交后 CPU 拉取归档；核验基线、备份 DB/文件，再定向安装及重启。无生产测试发布。
