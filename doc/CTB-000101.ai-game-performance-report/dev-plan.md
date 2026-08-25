# 开发计划

## 开发范围

新增独立 AI 游戏报表生成器、静态前端、SQLite 缓存、测试、Nginx 鉴权 location、systemd 刷新服务/timer 和全流程文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 冻结口径与评审 | Codex | `requirements.md`、`sa-review.md` | 已完成 |
| 生成器与缓存 | Codex | `ops/ai-game-performance/ai_game_performance_dashboard.py` | 已完成 |
| 前端/缓存契约 | Codex | `report.html`、Nginx 配置 | 已完成 |
| 单元与契约测试 | Codex | `ops/ai-game-performance/test_*.py` | 本地/服务器 19/19 通过 |
| 部署/回滚 | Codex | `deploy/*`、`deploy.md` | 已完成 |
| 生产验收 | 用户 | 任务看板 CTB-000101 | 待验收 |
| 验收缺陷 BUG-006 | Codex | 渠道明细双事实并列聚合、测试与发布 | 已上线；自然 timer、源库/缓存对账与生产浏览器终验通过 |

## 编译 / 构建命令

```bash
python -m py_compile ops/ai-game-performance/ai_game_performance_dashboard.py
python -m unittest discover -s ops/ai-game-performance -p "test_*.py" -v
python ops/ai-game-performance/validate_frontend_contract.py
git diff --check
```

## 风险与依赖

- 依赖 `/root/codex_test/opera_product_daily_dashboard.py` 提供现有只读 MySQL 命令；口令不进入 GitHub。
- 依赖现有 `/_tt_minis_report_auth` 飞书鉴权子请求。
- 初次全量刷新必须先发布到 `/mnt/data-disk` 阴影目录；通过对账后才能切换公开目录。
- 与 TT/归因报表共用 `flock`，避免重叠扫描只读库。

## 完成记录

- 2026-08-25：完成生产数据只读摸底、映射覆盖验证、隔离分支和需求/SA 评审。
- 2026-08-25：完成实现、本地代码评审、17 项单元/部署契约和前端语法验证；修复 BUG-001/BUG-002 本地部分。
- 2026-08-25：全量阴影发现并修复 BUG-003 日期格式、BUG-004 总播放时长语义；服务器与真实数据回归通过。
- 2026-08-25：生产登录态回归完成并加固 BUG-005 CSV Blob 生命周期；运行提交 `479398b` 已部署，自然 timer 成功，等待用户验收。
- 2026-08-25：验收反馈发现 BUG-006；确认 8 月 24 日渠道源只有 2 个渠道，而手工转化有 6 个渠道。前端改为复用两类日文件并列聚合，本地 19/19、前端契约和无双计行为测试通过。
- 2026-08-25：运行提交 `9804597` 上线；17:12 自然 timer 成功发布 `20260825T171308367560+0800`，源库/SQLite 逐字段一致，生产“仅渠道”聚合为 6 行。
