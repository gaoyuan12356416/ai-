# 开发计划

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 加法迁移和随机计划生成 | `features/tt_posts/core.py` | 已完成 |
| API 模式、次数与领取适配 | `features/tt_posts/service.py` | 已完成 |
| 多时间与随机模式 UI | `static/tt-post-pool.html` | 已完成 |
| 桌面 100% 缩放布局修复与回归 | `static/tt-post-pool.html`、`scripts/test_tt_post_pool_ui.py` | 已完成 |
| 核心、服务、UI 契约测试 | `scripts/test_tt_*.py` | 已完成 |
| GitHub 推送与生产发布 | immutable release | 已完成 |

## 验证命令

```powershell
python -m py_compile features/tt_posts/core.py features/tt_posts/service.py
python scripts/test_tt_posts_core.py
python scripts/test_tt_posts_service.py
python scripts/test_tt_post_pool_ui.py
python -m unittest scripts.test_tt_account_settings_ui scripts.test_tt_gpu_worker scripts.test_tt_posts_app_contract scripts.test_tt_post_direct_config_core scripts.test_tt_post_links scripts.test_tt_post_prepare_runner
node scripts/test_tt_drama_bridge.js
git diff --check
```

## 风险与依赖

- 生产数据库只允许在线备份后执行加法迁移。
- 调度器测试不得调用真实 Creator Info、GPU 或 TikTok 发布接口。
- UI 修复只调整 Grid 排版与响应式断点，不修改 DOM、事件、请求体或随机发布状态。
