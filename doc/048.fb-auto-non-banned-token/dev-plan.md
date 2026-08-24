# 开发计划

## 开发范围

统一 FB Page Token 的只读资格谓词，并补齐回归与发布证据。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 统一四处 SQL 谓词 | Codex | `features/fb_auto_posts/repositories.py` | 已完成 |
| 增加查询及动态重读合同测试 | Codex | `scripts/test_fb_auto_repositories.py`、`scripts/test_fb_auto_publisher.py` | 已完成 |
| 更新主需求/API 文档 | Codex | `doc/003.fb-page-auto-post/` | 已完成 |
| 本地回归 | Codex | FB 专项与 X/TT 合并基线 | 已完成 |
| 生产回归 | Codex | CPU release | 已完成 |
| GitHub-first 部署 | Codex | CPU `43.166.187.96` | 已完成 |

## 编译 / 构建命令

```bash
python -m py_compile features/fb_auto_posts/repositories.py
python -m unittest scripts.test_fb_auto_repositories
python -m unittest discover -s scripts -p "test_fb_auto*.py"
```

## 风险与依赖

- 生产 live gate 当前开启，部署时必须暂停运行型 timers，确认没有 running
  Graph 提交，再切换不可变 release。
- 不恢复旧 SQLite；代码回滚必须保留新发布事实。

## 完成记录

- 2026-08-24：完成生产只读基线，Page 组 62 当前 8 可发，新口径 12 可发。
- 2026-08-24：生产字段 `status` 为 NOT NULL、默认 0；21,210 行中 NULL=0。
- 2026-08-24：本地 24 项定向、129 项 FB、66 项 X/TT 基线全部通过。
- 2026-08-24：精确 release `d2a6e91f` 已部署；服务器 129+66 回归、
  health、只读 Page 池、SQLite 不变量及七个 timers 全部验收通过。
