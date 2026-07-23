# 开发计划

## 开发范围

在已验证 canary 基础上实现固定三账号每日发布、全局去重、后台日志和生产定时器，不改变 OAuth/软停用语义。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 生产/仓库/账号/数据源只读审计 | Codex | 服务器、SQLite、MySQL、Git | 已完成 |
| PM/SA/QA 文档与评审 | Codex | `doc/008.x-post-daily-scheduler/` | 已完成，待补生产证据 |
| 增量 schema、全局排重与批次状态 | Codex | `features/x_posts/service.py` | 已完成 |
| 选材与合规/媒体预检 | Codex | `features/x_posts/selector.py` | 已完成 |
| publish-by-queue 与日志查询 | Codex | OAuth Sidecar / client / backend | 已完成 |
| 每日 runner 与 systemd timer | Codex | `scripts/`, `deploy/` | 已完成 |
| AI 后台日志页面 | Codex | `static/`、admin API | 已完成 |
| 自动化测试、SA 代码评审、QA 回归 | Codex | `scripts/test_x_*.py` | 125 项离线回归通过，最终独立 GO |
| GitHub-first 部署、备份、timer 启用 | Codex | 43.166.187.96 | 待执行 |
| 新个人 Skill 创建、验证和同步 | Codex | `codex-personal-skills` | 已完成并推送 |

## 编译 / 构建命令

```powershell
python -m py_compile features/x_accounts/oauth_service.py features/x_accounts/client.py features/x_posts/*.py scripts/x_post_daily_runner.py scripts/wait_x_post_sidecar.py
python scripts/test_x_posts.py
python scripts/test_x_post_daily.py
python scripts/test_x_post_ledger.py
python scripts/test_x_accounts.py
python scripts/test_x_accounts_app_contract.py
python scripts/test_x_account_owner_backfill.py
node --check static/quick-nav.js
git diff --check
```

## 风险与依赖

- 依赖只读 MySQL 63350、素材公网 URL、ffprobe、X API、Sidecar 和 W2A/短链均可用。
- 生产主后台部署基线必须与 live composite 一致。
- 三个账号 Token 仅 Sidecar 专用用户可读；root-owned `0400` env 由 systemd 注入，不允许 ffprobe 继承。

## 完成记录

代码与个人 Skill 已完成；待生产部署后补充 commit、release、backup、timer next trigger。首轮真实发布仅由 2026-07-24 自然 timer 验收。
