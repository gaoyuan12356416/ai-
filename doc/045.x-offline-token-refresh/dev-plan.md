# 开发计划

## 开发范围

账号授权状态投影、自动执行刷新、最终发布刷新保护、X Auto 任务快照、两套账号页面及回归测试。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 状态与刷新核心 | Codex | `features/x_accounts/oauth_service.py` | 已完成 |
| 素材/短剧/人工排期预检 | Codex | `scripts/x_post_daily_runner.py` | 已完成 |
| X Auto 建 Run 前刷新 | Codex | `features/x_auto_posts/service.py` | 已完成 |
| UI 状态与说明 | Codex | `static/x-account-list.html`, `static/x-accounts.html` | 已完成 |
| 单元/契约/UI 回归 | Codex | `scripts/test_x*.py` | 已完成 |
| GitHub-first 部署与回滚验证 | Codex | `doc/045...`、生产 release/backup | 待执行 |

## 编译 / 构建命令

```bash
python -m compileall -q features scripts
python -m unittest discover -s scripts -p "test_x*.py"
node --check static/quick-nav.js
```

## 风险与依赖

- X OAuth Refresh Token 轮换语义；必须以服务用户/运行中 Sidecar 执行。
- X Auto、Sidecar、主 API 为独立运行时；只重启实际受影响服务。
- Nginx 直接服务 `/usr/share/nginx/html`，静态页需同步两处并校验哈希。

## 完成记录

- 2026-08-14：生产基线与定时器原状态已留档，建立独立修复分支和工作区。
- 2026-08-14：667 项 X 回归通过，2 项既有条件跳过；编译、JS 语法和差异检查通过。
