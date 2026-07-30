# 开发计划

## 开发范围

实现快速素材校验、durable intake、独立预制作 runner、合并素材池状态 UI，并保持既有 TikTok 发布状态机和生产 gate 不变。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 新增 intake schema、幂等与跨池去重 | Backend | `features/tt_posts/core.py` | 已实现，待最终回归 |
| 实现 FIFO claim、lease/renew/fencing、retry、原子 ready | Backend | `features/tt_posts/core.py` | 已实现，待最终回归 |
| preview 改为只读校验、add 改为 queued 入池 | Backend | `features/tt_posts/service.py` | 已实现，待最终回归 |
| 新增内部 prepare claim/renew/process API | Backend | `features/tt_posts/service.py` | 已实现，待最终回归 |
| 独立 one-shot prepare runner 与心跳 | Backend | `scripts/tt_post_prepare_runner.py` | 已实现，待最终回归 |
| 新增 systemd service/path/timer 与配置示例 | Ops | `deploy/tt-post-prepare.*`、`deploy/tt-post.env.example` | 已实现，待生产部署 |
| 前端快速校验、异步入池提示、状态轮询 | Frontend | `static/tt-post-pool.html` | 已实现，待浏览器验收 |
| 单元、契约与 UI 测试 | QA | `scripts/test_tt_posts_*.py`、`scripts/test_tt_post_pool_ui.py` | 待最终执行 |
| 生产备份、部署、只读 canary、回滚检查 | Ops | CPU 服务器 | 待执行 |

## 构建与验证命令

```powershell
python -m py_compile features/tt_posts/core.py features/tt_posts/service.py scripts/tt_post_prepare_runner.py
python -m unittest scripts.test_tt_posts_core scripts.test_tt_posts_service scripts.test_tt_post_prepare_runner scripts.test_tt_post_pool_ui scripts.test_tt_posts_app_contract
git diff --check
```

生产前额外执行仓库既有 TT Post 全量回归；最终命令和数量写入 `test-report.md`。

## 实施顺序

1. 先完成 schema 与 store 状态机，用临时 SQLite 验证幂等、FIFO、lease 和原子完成。
2. 调整 service：preview 只 resolve；add 只 resolve + freeze + insert + kick。
3. 加入 prepare 内部接口及独立 runner，确保只访问 `127.0.0.1:18829`。
4. 增加 systemd 单元和配置关系校验。
5. 更新 UI 与契约测试。
6. 完整回归、独立代码审查、生产备份。
7. GitHub-first 部署 CPU sidecar、静态页与 prepare runner；不改 GPU release。
8. 在发布 gates 全关闭状态下做快速校验、入池、后台 ready 的 canary，不点击立即发布。

## 风险与依赖

- 依赖 CPU 到 GPU `18830` 的现有内网/隧道可用性。
- 依赖只读 MySQL 素材库和账号快照。
- SQLite migration 在 TT sidecar 启动时执行；上线前必须备份数据库并校验新表与索引。
- systemd path 的 kick 是优化，不是正确性前提；timer 必须启用。
- 长 prepare 单元最大运行 9600 秒，部署/重启时不得在活跃制作中直接覆盖 current。

## 完成记录

待最终测试和生产验证后填写提交号、release 路径、备份路径、服务状态与 canary 记录。
