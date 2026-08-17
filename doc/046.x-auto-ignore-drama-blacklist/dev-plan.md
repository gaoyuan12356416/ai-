# 开发计划

## 开发范围

X Auto 两阶段选择器及其单元测试、需求与部署文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 删除初始剧黑名单拦截 | Codex | `features/x_auto_posts/selector.py` | 已完成 |
| 删除最终剧黑名单拦截 | Codex | `features/x_auto_posts/selector.py` | 已完成 |
| 保留并验证素材黑名单 | Codex | `scripts/test_x_auto_post_selector.py` | 已完成 |
| 本地/服务器回归与部署 | Codex | tests/deploy | 本地完成，待部署 |

## 编译 / 构建命令

```bash
python -m py_compile features/x_auto_posts/selector.py
python scripts/test_x_auto_post_selector.py
python scripts/test_x_auto_post_service.py
python scripts/test_x_auto_post_publisher.py
```

## 风险与依赖

- 依赖现有只读 MySQL 黑名单快照和 X Auto SQLite。
- 部署时不得通过 run-now 或真实 Post 验证。

## 完成记录

- 2026-08-17：选择器 22/22、本地 X 回归 670 项中 668 通过、2 项条件跳过、0 失败。
- Python 编译和 `git diff --check` 通过。
