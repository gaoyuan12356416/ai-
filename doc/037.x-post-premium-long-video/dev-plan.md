# 开发计划

## 开发范围

账号会员快照、素材池长视频路由、队列时长审计、长视频上传类别、GPU 双时长策略、管理页展示、测试、文档和 CPU/GPU GitHub-first 部署。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 会员字段与 DTO | Codex | `features/x_accounts/oauth_service.py`、账号 UI/测试 | 已完成 |
| 长视频预检/分配/发布 | Codex | `features/x_posts/service.py`、`selector.py`、runner/测试 | 已完成 |
| GPU 时长策略 | Codex | `features/x_posts/media_repair.py`、worker 测试/配置示例 | 已完成 |
| PM/SA/QA 文档 | Codex | `doc/037.x-post-premium-long-video/` | 已完成（待补生产证据） |
| GitHub/CPU/GPU 部署与验证 | Codex | 精确 release、SQLite 备份、服务健康 | 待处理 |

## 编译 / 构建命令

```bash
python -m py_compile features/x_accounts/oauth_service.py features/x_posts/service.py features/x_posts/media_repair.py scripts/x_post_daily_runner.py scripts/x_post_schedule_runner.py scripts/x_post_media_repair_worker.py
python scripts/test_x_accounts.py
python scripts/test_x_posts.py
python scripts/test_x_post_daily.py
python scripts/test_x_post_schedule_runner.py
python scripts/test_x_post_media_repair.py
python scripts/test_x_post_multi_schedule_ui.py
python scripts/test_x_accounts_app_contract.py
git diff --check
```

## 风险与依赖

- 依赖 X `/2/users/me` 返回认证用户专属 `subscription_type`。
- 依赖 X `amplify_video` chunked upload 对有权益个人账号的实际支持；不通过未授权真实发帖验证。
- CPU/GPU 必须部署同一提交和同一 repair profile，否则修复请求会失败关闭。

## 完成记录

- 2026-08-10：完成生产只读基线、正式账号/排期/DB schema 核验和官方字段/时长合同核验。
- 2026-08-10：完成代码、UI、迁移、GPU 双策略和 381 项 X 全量回归；等待 GitHub-first 双端部署。
