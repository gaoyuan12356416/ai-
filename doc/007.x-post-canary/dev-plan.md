# 开发计划

## 开发范围

实现一个可审计、可回滚、默认不调度的 X Post 灰度发布闭环。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 昨日素材与合规数据只读审计 | Codex | 生产只读 SQL / 素材预检 | 已完成 |
| 队列、日志、URL 与静态短链 | Codex | `features/x_posts/` | 已完成 |
| X 媒体上传与 Create Post 客户端 | Codex | `features/x_posts/` | 已完成 |
| 账号锁内刷新与 canary 内部接口 | Codex | `features/x_accounts/oauth_service.py` | 已完成 |
| 自动化测试与回归 | Codex | `scripts/test_x_posts.py`, `scripts/test_x_accounts.py` | 55/55 通过 |
| GitHub 提交、生产副本演练与部署 | Codex | release / systemd / 数据盘 | 待执行 |
| 一条真实 Post 与日志核验 | Codex | X API / SQLite / 公网链接 | 待执行 |

## 编译 / 构建命令

```bash
python -m py_compile features/x_accounts/oauth_service.py features/x_posts/*.py
python scripts/test_x_posts.py
python scripts/test_x_accounts.py
```

## 风险与依赖

- 依赖只读业务数据库、素材源、X API、`ai.yingliangads.com` Nginx 和 Dramawave W2A 均可用。
- 不允许把任何 OAuth Token 带到主应用或命令行输出。
- 生产部署前需确认 `/mnt/data-disk` 是预期持久盘，备份 SQLite 和 Token 文件 hash/权限。

## 完成记录

候选审计和本地开发/回归已完成；待补全 commit、部署 release、日志 ID 和 post ID。
