# 开发计划

## 开发范围

扩展既有 `TT 个号管理`，新增批量能力检测与原子保存；不修改发布池、GPU 和真实发布门禁。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 原子批量存储 | Codex | `features/tt_posts/core.py` | 已完成 |
| 批量能力检测与保存服务 | Codex | `features/tt_posts/service.py` | 已完成 |
| AI 后台同源代理与审计 | Codex | `app.py` | 已完成 |
| 批量管理交互 | Codex | `static/tt-account-settings.html` | 已完成 |
| 自动化测试 | Codex | `scripts/test_tt_*.py` | 已完成 |
| GitHub/CPU 部署与浏览器验收 | Codex | release、服务、公开页面 | 待开始 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py features/tt_posts/core.py features/tt_posts/service.py
python scripts/test_tt_posts_core.py
python scripts/test_tt_posts_service.py
python scripts/test_tt_posts_app_contract.py
python scripts/test_tt_post_pool_ui.py
python scripts/test_tt_account_settings_ui.py
node --check static/quick-nav.js
git diff --check
```

## 风险与依赖

- 依赖现有账号快照、CPU sidecar 和 GPU `creator_info` 内部接口。
- 不读取、显示或记录 Token。
- 生产验收只读，不提交真实批量设置。

## 完成记录

- 2026-07-29：完成批量选择、最多 50 个账号、共同能力检测和共享表单。
- 2026-07-29：完成每账号独立版本校验和 SQLite 原子批量保存。
- 2026-07-29：完成后台权限、同源代理、安全审计和自动化回归。
- 生产发布和只读浏览器验收在 GitHub 提交后执行。
