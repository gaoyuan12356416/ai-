# 开发计划

| 任务 | 模块 | 状态 |
| --- | --- | --- |
| 账号时长预检 | publisher/selector | 完成 |
| 提前调度和准时发布闸门 | service/core | 完成 |
| 制作/发布 lane 拆分 | runner/service/core | 完成 |
| GPU 阶段耗时 | GPU worker/event | 完成 |
| 离线回归、主应用契约 | scripts/tests | 完成 |
| GitHub-first 发布与自然任务验证 | CPU/GPU 生产 | 待执行 |

构建与测试命令见 `test-report.md`。生产部署必须先等待当前 run 30 收尾并备份 release 指针、env 和 SQLite。
