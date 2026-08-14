# 开发计划

## 开发范围

精确源数据错误、修复结果明细、manual runner 归一、弹窗中文展示、测试与部署文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 拆分源数据拒绝原因 | Codex | `features/x_posts/selector.py` | 已完成 |
| 拆分修复结果消息 | Codex | `features/x_posts/media_repair.py` | 已完成 |
| 归一手动 run 错误 | Codex | `scripts/x_post_manual_runner.py` | 已完成 |
| 简化弹窗展示 | Codex | `static/x-post-material-pool.html` | 已完成 |
| 自动化测试与部署 | Codex | `scripts/test_x_post_*.py`、生产 CPU/GPU | 已完成 |

## 编译 / 构建命令

```bash
python -m py_compile features/x_posts/selector.py features/x_posts/media_repair.py scripts/x_post_manual_runner.py
python -m unittest scripts.test_x_post_material_pool_selector scripts.test_x_post_manual_runner scripts.test_x_post_media_repair scripts.test_x_post_multi_schedule_ui
```

## 风险与依赖

- GPU worker 与 CPU runner 必须部署同一 Git commit。
- Nginx 公共静态目录与主应用静态目录必须同步。

## 完成记录

- 2026-08-14：第一轮 59 项针对性测试通过。
- 2026-08-14：完整回归通过，commit `eb15510b75d9045d5b660b57f3971137044a3523` 已部署到 CPU/GPU；生产零发帖验收通过。
