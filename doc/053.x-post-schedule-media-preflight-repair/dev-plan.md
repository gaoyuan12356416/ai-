# 开发计划

## 开发范围

恢复 material schedule 的完整媒体预检与 GPU repair，补齐自动化、文档、部署和回滚证据。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 生产/代码基线冻结 | Codex | Git/SQLite/systemd | 已完成 |
| 预检与 repair 接入 | Codex | `scripts/x_post_schedule_runner.py` | 已完成 |
| codec/dimensions/深扫回归 | Codex | `scripts/test_x_post_schedule_runner.py` | 已完成 |
| 全量 X 回归与代码评审 | Codex | `scripts/test_x*.py` | 已完成 |
| GitHub-first 部署 | Codex | CPU immutable release | 待执行 |

## 编译 / 构建命令

```powershell
python -m py_compile scripts\x_post_schedule_runner.py
python -m unittest scripts.test_x_post_schedule_runner
python -m unittest discover -s scripts -p "test_x*.py"
git diff --check
```

## 风险与依赖

- 依赖 CPU 到 GPU repair endpoint 健康且 profile 为 v5。
- 依赖排期环境配置 repair URL/token/profile 和足够 repair budget。
- 不允许真实 X 测试 Post。

## 完成记录

- 2026-08-24：从生产精确 commit `2f9d31d8...` 建立独立 worktree。
- 2026-08-24：focused 55/55、X 全量 752（通过 750、条件跳过 2）通过。
