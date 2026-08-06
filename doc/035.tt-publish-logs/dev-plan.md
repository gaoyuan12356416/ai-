# 开发计划

## 开发范围

实现只读聚合接口、独立日志页面、导航调整、旧页面日志移除以及自动化测试；不部署生产。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 旧账本只读日志查询 | Codex | `features/tt_auto_posts/legacy_reader.py` | 已完成 |
| 自动账本日志查询与聚合 | Codex | `core.py`、`service.py`、`client.py` | 已完成 |
| 主 API 代理 | Codex | `app.py` | 已完成 |
| 独立页面与导航 | Codex | `static/tt-publish-logs.*`、导航文件 | 已完成 |
| 发布池日志移除 | Codex | `static/tt-post-pool.html` | 已完成 |
| 自动化验证 | Codex | `scripts/test_tt_publish_logs*.py` | 已完成 |

## 编译 / 构建命令

```powershell
python -m py_compile features/tt_auto_posts/*.py app.py
python -m unittest scripts.test_tt_publish_logs_service scripts.test_tt_publish_logs_ui scripts.test_tt_auto_publish_app_contract scripts.test_tt_auto_publish_ui scripts.test_tt_auto_post_store scripts.test_tt_auto_post_service scripts.test_tt_post_pool_ui scripts.test_tt_posts_app_contract scripts.test_tt_posts_core scripts.test_tt_posts_service
git diff --check
```

## 风险与依赖

- 依赖两个 SQLite 账本在同一 CPU 主机可读。
- 主 API、自动发布 sidecar 和静态资源需要作为同一发布版本部署。
- 页面操作继续调用旧队列接口，不能改变其权限或写入语义。

## 完成记录

2026-08-06 完成实现。统一日志聚合保持两个账本只读，旧发布池后端与自动发布状态机均未修改；339 项 TT 相关回归通过，并完成桌面、详情、来源筛选和 390×844 窄屏浏览器验收。
