# 开发计划

## 开发范围

在账号、X Post 调度、X Auto、管理 UI 和部署工具中实现语言路由，保持历史冻结事实不变。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 生产只读审计 | Codex | 服务、SQLite、定时器、现有模板/绑定 | 完成 |
| 语言模型与账号 API | Codex | `features/x_accounts`、`app.py` | 完成 |
| 素材/短剧/中继路由 | Codex | `features/x_posts`、调度脚本 | 完成 |
| X Auto 全链路校验 | Codex | `features/x_auto_posts`、模板 UI | 完成 |
| 账号列表与运营提示 | Codex | `static` | 完成 |
| 迁移/回滚工具 | Codex | `scripts/migrate_x_account_drama_languages.py` | 完成 |
| 测试与文档 | Codex | `scripts/test_x*`、`doc/044...` | 完成 |

## 编译 / 构建命令

```powershell
python -m compileall -q features/x_accounts features/x_posts features/x_auto_posts scripts/x_post_daily_runner.py scripts/x_post_schedule_runner.py scripts/migrate_x_account_drama_languages.py app.py
git diff --check
python -m unittest discover -s scripts -p 'test_x*.py'
```

## 风险与依赖

- 依赖账号数据库与 X Post 数据库的现有 SQLite 可用性。
- 生产迁移需要在相关服务停止后执行并生成独立备份。
- 静态文件需同步到应用目录和 Nginx 公共目录。
- 不允许以真实发帖作为部署测试。

## 完成记录

- 2026-08-14：实现完成；659 个 X 相关离线测试整体通过（657 通过、2 个环境测试跳过）。
