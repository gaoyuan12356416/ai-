# 开发计划

## 开发范围

X 素材/短剧排程候选选择、内部计划冻结、存储计数、测试与生产发布。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 固定需求与账本语义 | Codex | `doc/051.x-post-partial-capacity` | 完成 |
| 调度器支持可用子集 | Codex | `scripts/x_post_schedule_runner.py` | 完成 |
| 短剧选择支持账号缺口 | Codex | `features/x_posts/drama_selector.py` | 完成 |
| 存储层原子冻结部分计划 | Codex | `features/x_posts/service.py` | 完成 |
| 修复跨页扫描与历史异常复检 | Codex | runner、store、专项测试 | 完成 |
| 单元/集成/副本回归 | Codex | `scripts/test_x_post_*.py` | 完成 |
| GitHub-first 生产部署 | Codex | CPU X 发布服务 | 完成 |

## 编译 / 构建命令

```powershell
python -m py_compile scripts/x_post_schedule_runner.py features/x_posts/service.py features/x_posts/drama_selector.py
python -m unittest scripts.test_x_post_drama_selector scripts.test_x_post_schedule_runner scripts.test_x_post_material_random_relay scripts.test_x_post_multi_schedule_store
```

## 风险与依赖

- 依赖 CPU Sidecar、主 API、SQLite 和自然 systemd timer。
- 部署时不得覆盖生产 SQLite、Token 或在途队列。
- 真实验收优先使用自然排程，不创建测试 Post。

## 完成记录

- 2026-08-20：核心实现完成，专项回归 138/138、完整 X 回归 715/715（2 条条件跳过）通过。
- 2026-08-20：复查补修跨页候选与历史异常恢复，修复专项 106/106、完整 X 回归 718/718（2 条条件跳过）通过；待生产副本和部署验收。
- 2026-08-20：精确 release 服务器四模块 141/141、生产副本正反例及部署后分钟 timer 通过，发布 commit `63ec8f36aa1132d7196dd7933369c6e1c7ec05a1`。
