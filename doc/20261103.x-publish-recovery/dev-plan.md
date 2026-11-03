# 开发计划

## 开发范围

回执核对修复、旧手动任务精确授权、媒体请求传输改进，以及不会触发真实测试帖的本地回归。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 线上只读基线与精确恢复清单 | 主代理 | 私有审计输出 | 已取得部分证据，执行前刷新 |
| 原生成功回执修复及晚到回执观察 | 回执实现代理 | x_auto_posts/receipt_reconciliation.py、publisher.py | 已实现，持续审查 |
| 旧手动任务授权及正常领取 | 计划审查代理 | x_manual_recovery.py、recover_x_unstarted_manual.py、x_posts/service.py | 已实现，17 项相关测试通过 |
| 定时批次零写入预检恢复 | 计划审查代理 | x_schedule_recovery.py、recover_x_zero_write_schedules.py、x_posts/service.py | 已实现，11 项新增测试通过，含已有调度/账号预检组合 169 项通过 |
| 媒体分块、超时及回归 | 主代理 | pipeline_dispatch/executors.py、test_pipeline_x_upload_transport.py | 已实现 |
| 独立并发/去重/心跳审查 | 计划审查代理 | 回执、传输及 worker | 35 项组合测试通过；汇总结束时间问题已闭环 |
| GitHub 版本发布、备份、精确 apply、线上验收 | 主代理 | 实际部署路径和私有计划 | 由部署报告记录 |

## 编译 / 构建命令

```bash
python -m py_compile features/pipeline_dispatch/x_manual_recovery.py scripts/recover_x_unstarted_manual.py features/x_posts/service.py
python -m unittest scripts.test_x_unstarted_manual_recovery scripts.test_pipeline_x_planning
python -m unittest scripts.test_x_auto_receipt_reconciliation scripts.test_pipeline_x_upload_transport scripts.test_pipeline_worker
git diff --check
```

## 风险与依赖

在独立工作树开发，经 GitHub 留存版本后部署；发布前检查与正在进行的独立改动的组件交集。线上数据库和精确计划只能在私有受限路径保存。备份应校验完整性与摘要；应用使用最新计划和事务复核。部署不得通过新建测试帖验证。

## 完成记录

- 2026-11-03：旧手动恢复与 X 计划相关 17 项测试通过。
- 2026-11-03：独立复核回执修复、媒体传输及 worker 共 35 项测试通过。
- 2026-11-03：零写入定时恢复、既有 schedule store 及正常账号预检组合 169 项测试通过；未执行线上恢复写入。
- 线上版本、操作批次及最终回执数量由主代理补入部署及测试报告。
