# 开发计划

## 开发范围

存储迁移、事务补偿方法、受锁 CLI、单元/回归测试、部署与回滚文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 存储与事务守卫 | Codex | `features/x_posts/service.py` | 完成 |
| CLI 与原子报告 | Codex | `scripts/x_post_schedule_drama_scope_compensate.py` | 完成 |
| 单元与回归测试 | Codex | `scripts/test_x_post_schedule_drama_scope_compensate.py` | 完成 |

## 编译 / 构建命令

```bash
python -m py_compile features/x_posts/service.py scripts/x_post_schedule_drama_scope_compensate.py
python -m unittest scripts.test_x_post_schedule_drama_scope_compensate -v
python -m unittest discover -s scripts -p "test_x*.py"
```

## 风险与依赖

依赖当前 drama 配置已保存为旧范围的严格子集，并要求 scheduler 全局锁可用。

## 完成记录

2026-08-19：实现与聚焦测试完成，待生产发布验收。
