# 开发计划

## 开发范围

实现状态列可见性、补发子批次存储/Sidecar/runner、测试、GitHub-first 部署与一次受控生产补发。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| UI 列位与缓存 | Codex | static、Nginx | 已完成 |
| 子批次存储 | Codex | `features/x_posts/service.py` | 已完成 |
| Sidecar 内部接口 | Codex | `features/x_accounts/*` | 已完成 |
| 手工 runner/oneshot | Codex | `scripts/x_post_catchup_runner.py`、`deploy/x-post-catchup.service` | 已完成 |
| 自动化测试/审查 | QA/SA | `scripts/test_x*.py`、文档 | 已完成本地测试，待生产验收 |
| 生产部署与补发 | Codex | CPU 服务器 | 待执行 |

## 编译 / 构建命令

```powershell
python -X utf8 -m py_compile features\x_posts\service.py features\x_accounts\client.py features\x_accounts\oauth_service.py scripts\x_post_catchup_runner.py
python -X utf8 -m unittest discover -s scripts -p "test_x*.py"
node --check static\quick-nav.js
git diff --check
```

## 风险与依赖

- 真实 X 写入依赖六个账号仍可刷新以及 X API 稳定。
- 生产建批前必须完成在线 SQLite 备份、Token 非敏感 hash/mode 清单和精确 release 备份。

## 完成记录

本地 `test_x*.py` 共 229 项通过；Python 编译、JSON、Node 静态检查和
`git diff --check` 通过。生产部署和六账号补发结果在完成后写入
`deploy.md` 与 `test-report.md`。
