# 开发计划

## 开发范围

在独立 `codex/ad-control-v3-live-pause-copy` clean worktree 内完成 V3 FB 写链路、ads_ai 迁移、动态 UI、runner 和发布回滚，不改 V2 行为。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 真实 Graph pause/copy 与回读 | `live_execution.py` | 完成 |
| intent/created_data/lineage | SQL + `live_execution.py` | 完成 |
| 手动执行 API/UI | `service.py`、`routes.py`、`app.js` | 完成 |
| 账号时区调度 | `scheduler.py`、runner、systemd | 完成 |
| 故障注入和 V3 回归 | `tests/test_ad_control_v3_*` | 完成 |
| GitHub-first 部署/Canary | 生产检查点与发布记录 | 待执行 |

## 编译 / 构建命令

```bash
python3 -m compileall -q features/ad_control_v3 scripts/ad_control_v3_runner.py
node --check features/ad_control_v3/assets/app.js
python3 -m unittest discover -s tests -p 'test_ad_control_v3*.py'
```

## 风险与依赖

- 依赖 Meta Graph v25.0、产品 `ads_apps_setting.default_user` Token、ads_ai writer 63353。
- Meta POST 不做自动重试；丢失响应按不确定写隔离。
- DDL 和系统服务必须先备份并由 exact commit 发布。

## 完成记录

本地代码和 150 项 V3 回归已通过；生产结果写入 `test-report.md`。
