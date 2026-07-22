# 开发计划

## 开发范围

新增独立同步脚本、目标 DDL、systemd service/timer、环境模板、自动测试和部署文档；不修改现有 HTTP 服务。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 源/目标边界与关联核验 | Codex | SocialKit / ads_ai metadata | 完成 |
| 同步脚本与安全保护 | Codex | `scripts/sync_socialkit_tiktok_accounts.py` | 完成 |
| 单元测试 | Codex | `scripts/test_sync_socialkit_tiktok_accounts.py` | 13/13 通过 |
| DDL 与配置模板 | Codex | `doc/013.../*.sql`, `deploy/*.env.example` | 完成 |
| systemd 小时任务 | Codex | `deploy/*.service|timer` | 完成，待部署 |
| GitHub-first 生产部署 | Codex | commit/release/systemd/DDL | 待执行 |

## 编译 / 构建命令

```powershell
python -m py_compile scripts\sync_socialkit_tiktok_accounts.py scripts\test_sync_socialkit_tiktok_accounts.py
python scripts\test_sync_socialkit_tiktok_accounts.py
git diff --check
```

## 风险与依赖

- CPU 服务器需保留 `PyMySQL` 与网络连通性。
- `/etc/socialkit-tiktok-account-sync.env` 必须 root:root 0600，真实密码不得进入 Git。
- DDL 仅在维护步骤通过 63353 执行一次。

## 完成记录

2026-07-22 本地 Python 编译、13 个单元测试、diff check 和数据库密码扫描通过。待补充 GitHub commit、生产备份点和线上验证。
