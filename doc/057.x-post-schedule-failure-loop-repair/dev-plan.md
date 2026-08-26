# 开发计划

## 开发范围

修复素材 FIFO/重试/终态、查询重连、跨午夜租约、短剧失败媒体恢复，并完成 GitHub-first 生产发布。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 根因与生产止血 | Live audit | 生产 ledger/journal/systemd | 完成 |
| 查询安全错误 | Selector worker | selector.py + selector tests | 完成 |
| FIFO/runner/lease | 主开发 | runner/service/oauth | 完成；全回归通过 |
| 短剧恢复 | Recovery worker | service + store tests | 完成；专用回归通过 |
| 独立 QA | QA agent | 全量 X 测试与 review | 完成；无剩余 P0/P1 |
| GitHub/部署/验收 | 主开发 | release + systemd + report | 待执行 |

## 编译 / 构建命令

```bash
python -m py_compile features/x_posts/service.py features/x_posts/selector.py \
  features/x_accounts/oauth_service.py scripts/x_post_schedule_runner.py
```

## 风险与依赖

- 生产 SQLite 必须在线备份后才运行迁移。
- 定时器在部署与 manifest 验证完成前保持 inactive。
- 短剧媒体修复依赖 GPU 18820 健康，但不能触发 X。

## 完成记录

- 2026-08-26：完成线上只读审计和止血。
- 2026-08-26：完成素材容量证明、查询重连、计划围栏、租约和短剧恢复实现。
- 2026-08-26：独立审查发现并修复 schedule DTO 围栏丢失与容量证明可事后凑满两个 P1。
- 2026-08-26：最终相关聚焦测试 181/181、完整 X 回归 796/796 通过；2 项环境相关用例按既有条件跳过。
