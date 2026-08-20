# 开发计划

## 开发范围

定时素材池/短剧池从“全批媒体预检后建队”改为“轻量候选冻结后逐条发布时校验”。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 轻量素材/短剧候选与失败继续 | Codex | selector、schedule runner | 已完成 |
| deferred queue、actual publish、失败隔离 | Codex | X post store、OAuth sidecar | 已完成 |
| 自动化测试与独立代码复核 | QA/SA | `scripts/test_x*.py`、评审文档 | 已完成 |
| GitHub-first 部署、同批次续跑、生产验收 | Codex | CPU server immutable release | 待执行 |

## 编译 / 构建命令

```powershell
python -m py_compile features/x_posts/selector.py features/x_posts/service.py features/x_accounts/oauth_service.py scripts/x_post_schedule_runner.py
python -m unittest scripts.test_x_post_schedule_runner scripts.test_x_post_multi_schedule_store scripts.test_x_posts scripts.test_x_accounts
python -m unittest discover -s scripts -p "test_x*.py"
git diff --check
```

## 风险与依赖

- 生产旧 runner 必须仅在 0 queue/log/attempt/unknown 时终止。
- 部署需要 Sidecar/主 API 共用相同 service 代码和 SQLite 加列迁移。
- 不执行额外真实 X canary；续跑 run 271 是唯一授权发布范围。

## 完成记录

- 2026-08-20 16:00：保护检查通过，旧 15:11 runner 与 schedule timer 已停止；run 271 保持 claimed、零 X 触达。
- 2026-08-20：实现与独立 QA 完成；专项 259/259、X 全量 729 通过（2 条条件 skip），`py_compile` 与 `git diff --check` 通过。
