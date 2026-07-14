# 开发计划

## 开发范围

完成 X OAuth sidecar 多账号化、AI 后台代理 API、模块权限、导航、独立管理页面、部署配置与验证。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 生产/仓库基线核对 | Codex | live + Git worktree | 已完成 |
| OAuth sidecar 多账号与内部 API | Codex | `features/x_accounts/oauth_service.py` | 已完成 |
| AI 后台 loopback client/API/权限 | Codex | `features/x_accounts/client.py`, `app.py` | 已完成 |
| 页面与导航 | Codex | `static/x-accounts.html`, nav files | 已完成 |
| 自动化测试与安全扫描 | Codex | `scripts/test_x_accounts.py` | 已完成 |
| GitHub-first 部署与浏览器验收 | Codex | deploy/docs/server | 待执行 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py features\x_accounts\client.py features\x_accounts\oauth_service.py
python scripts\test_x_accounts.py
node --check static\quick-nav.js
git diff --check
```

## 风险与依赖

- 需要保留生产复合 `app.py` 的全部热修复。
- 需要生成仅服务器保存的 `X_POST_AUTOMATION_INTERNAL_TOKEN`。
- 真实验收依赖用户在 X 页面同意授权。

## 完成记录

本地 16 项 X功能测试、Python编译、JS/JSON语法和 diff检查均通过；生产部署与真实浏览器授权待执行。
