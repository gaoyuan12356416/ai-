# 开发计划

## 开发范围

V3 复制执行边界、V3 时间工具、API 序列化、日志查询、前端日期格式化、runner 输出及相关测试/文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 线上/分支基线合并 | Codex | Git、共享 app.py | 已完成 |
| UTC+8 公共时间工具 | Codex | `time_utils.py` | 已完成 |
| 三层复制重命名 | Codex | `live_execution.py` | 已完成 |
| API/日志/日期筛选 | Codex | `routes.py`、`repository.py`、`service.py` | 已完成 |
| UI/runner 时区 | Codex | `app.js`、runner | 已完成 |
| 自动化验证 | Codex | tests | 已完成 |
| 生产验证 | Codex | 服务器 | 待发布 |

## 编译 / 构建命令

```bash
python -m py_compile app.py features/ad_control_v3/*.py scripts/ad_control_v3_runner.py
node --check features/ad_control_v3/assets/app.js
python -m unittest <V3 精确测试集合>
git diff --check
```

## 风险与依赖

- Meta 名称更新为额外写请求，必须保持 PAUSED、无重试和逐级回读。
- 线上共享 monolith 存在并行 playable 发布，部署 source commit 必须使用 `4ecaa75` 或其完整后继，禁止从旧 `3b2e2ca` 覆盖。
- MySQL DATETIME 无时区，必须统一视为 UTC 审计存储；Meta `start_time` 仍保留账号本地墙钟语义。

## 完成记录

- 2026-07-17：创建并推送整合基线 `7f9cdf0`，同时保留 playable `4ecaa75` 与 V3 UI `63f1d1c`。
- 2026-07-17：完成三层命名、UTC+8 API/UI/查询/runner 实现及本地自动化回归。
