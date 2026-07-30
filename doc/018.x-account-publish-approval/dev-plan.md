# 开发计划

## 开发范围

数据库幂等迁移、账号 DTO、管理员写接口、列表页交互、发布安全闸门及自动化回归。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 新增许可字段与迁移 | Codex | `oauth_service.py` | 完成 |
| 有效资格和最终发布闸门 | Codex | `oauth_service.py` | 完成 |
| 管理员 API 与审计 | Codex | `client.py`、`app.py` | 完成 |
| 列表页复选框 | Codex | `x-account-list.html` | 完成 |
| 自动化回归 | Codex | `scripts/test_x_accounts*.py` | 完成 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py features/x_accounts/client.py features/x_accounts/oauth_service.py
python -m unittest scripts.test_x_accounts scripts.test_x_accounts_app_contract scripts.test_x_post_multi_schedule_ui
```

## 风险与依赖

依赖现有 X sidecar 与主服务内部鉴权。上线迁移后所有账号默认不可发布，必须由管理员显式启用。

## 完成记录

2026-07-30：代码与 354 项 X 全量回归通过，待按 GitHub 精确提交部署。
