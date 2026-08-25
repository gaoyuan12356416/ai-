# 开发计划

## 开发范围

X selector、随机排期语言分配、香港 GPU systemd/依赖基线、回归测试与生产对账。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 移除标签 gate | Codex | `features/x_posts/selector.py` | 完成 |
| 修复语言容量误报 | Codex | `scripts/x_post_schedule_runner.py` | 完成 |
| 增补回归 | Codex | `scripts/test_x_post_*.py` | 完成 |
| 香港 GPU 部署基线 | Codex | `deploy/x-post-media-repair-hk*` | 完成 |
| 生产迁移与状态处理 | Codex | CPU/旧 GPU/香港 GPU | 待部署 |

## 编译 / 构建命令

```bash
python -m compileall -q features scripts
python -m unittest scripts.test_x_post_daily scripts.test_x_post_material_pool_selector scripts.test_x_post_schedule_runner scripts.test_x_post_material_random_relay scripts.test_x_post_media_repair scripts.test_x_post_media_repair_backfill
```

## 风险与依赖

- 香港 GPU 需 Python 3.9、COS SDK、现有 COS 密钥和专用反向隧道密钥。
- CPU 18820 只能由一条生产隧道占用；切换需要短维护窗口。
- 媒体重制可能耗时，但显式 ID 上限为 8，且不会创建 X 队列。

## 完成记录

- 2026-08-25：完成代码变更与 164 项聚焦测试。
