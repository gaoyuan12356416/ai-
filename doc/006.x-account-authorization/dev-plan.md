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
| GitHub-first 生产部署 | Codex | deploy/docs/server | 已完成 |
| 登录态浏览器与真实 X OAuth 验收 | 用户 + Codex | `https://ai.yingliangads.com/x-accounts.html` | 待用户授权 |

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

本地 16 项 X功能测试、Python编译、JS/JSON语法和 diff检查均通过。提交 `eccabcb0d49714efa90403b140c0d2f77e5182dc` 已从 GitHub 精确检出到 `/root/releases/ai-x-account-authorization-eccabcb0d497` 并完成生产部署；备份位于 `/root/backups/drama_material_service/20260714T041337Z-x-accounts-eccabcb`。生产服务、API边界、Cookie鉴权、日志脱敏和文件模式验证通过。真实 X OAuth 必须由用户在 X 官方页面确认，授权后列表与 Token 生命周期验收仍待执行。
